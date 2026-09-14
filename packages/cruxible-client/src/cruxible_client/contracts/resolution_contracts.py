"""Governed tests of exact Claim versions, independent of an investigating method."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    Sha256Value,
    artifact_bytes_for_path,
    artifact_path_matches,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    claim_artifact_digest,
    claim_statement_digest,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedures.windows import (
    BoundObservationWindowV1,
    ObservationWindowV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.resolution_rules import (
    PredictionObservationSelectorV1,
    PredictionRuleV1,
)

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")


class ClaimVersionReferenceV1(_ContractModel):
    identity: ArtifactIdentity
    artifact_digest: str
    statement_digest: str
    coordinate: AcceptedCoordinate

    @field_validator("artifact_digest", "statement_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _claim(self) -> ClaimVersionReferenceV1:
        if self.identity.kind != "Claim":
            raise ValueError("resolution hypothesis must be a Claim")
        return self

    def verify(self, claim: ClaimArtifactAny) -> None:
        if (
            claim.identity != self.identity
            or claim_artifact_digest(claim).tagged != self.artifact_digest
            or claim_statement_digest(claim.statement).tagged != self.statement_digest
        ):
            raise PlaybillFormatError("resolution hypothesis does not reproduce its exact Claim")


class ResolutionContractV1(_ContractModel):
    artifact_format: Literal["playbill-resolution-contract-v1"] = "playbill-resolution-contract-v1"
    identity: ArtifactIdentity
    hypothesis: ClaimVersionReferenceV1
    observation: PredictionObservationSelectorV1
    rule: PredictionRuleV1
    window: ObservationWindowV1
    outcome_class: str = "prediction-correctness"
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @model_validator(mode="after")
    def _identity(self) -> ResolutionContractV1:
        if self.identity.kind != "ResolutionContract" or not _NAME.fullmatch(self.identity.name):
            raise ValueError("ResolutionContract identity is not path-addressable")
        if not _NAME.fullmatch(self.outcome_class):
            raise ValueError("resolution outcome class must be canonical")
        return self

    def verify_hypothesis(self, claim: ClaimArtifactAny) -> None:
        from cruxible_client.contracts.claims import LiteralClaimObject

        self.hypothesis.verify(claim)
        obj = claim.statement.object
        if not isinstance(obj, LiteralClaimObject):
            raise PlaybillFormatError(
                "mechanical resolution requires a canonical literal hypothesis"
            )
        if self.rule.operator in {"threshold", "presence"} and not isinstance(obj.value, bool):
            raise PlaybillFormatError("threshold and presence hypotheses must predict a boolean")

    @property
    def pins(self) -> tuple[ArtifactPin, ...]:
        # The historical hypothesis is evidence, not a requirement that the
        # currently live Claim retain the original version.
        return ()


def resolution_contract_path(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise PlaybillFormatError("ResolutionContract identity is not path-addressable")
    return f"resolution-contracts/{name}.json"


def render_resolution_contract(contract: ResolutionContractV1) -> bytes:
    return pretty_canonical_bytes(contract.model_dump(mode="json"))


def resolution_contract_digest(contract: ResolutionContractV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest, "playbill-resolution-contract-artifact-v1", contract.model_dump(mode="json")
    )


def parse_resolution_contract(
    content: bytes, *, path: str, codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC
) -> ResolutionContractV1:
    try:
        contract = ResolutionContractV1.model_validate(json.loads(content))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlaybillFormatError("ResolutionContract failed strict validation") from exc
    if not artifact_path_matches(
        resolution_contract_path(contract.identity.name), path, codec=codec
    ):
        raise PlaybillFormatError("ResolutionContract identity/path disagreement")
    if artifact_bytes_for_path(render_resolution_contract(contract), path, codec=codec) != content:
        raise PlaybillFormatError("ResolutionContract bytes are not canonical")
    return contract


class ResolutionContractReferenceV1(_ContractModel):
    identity: ArtifactIdentity
    artifact_digest: str
    coordinate: AcceptedCoordinate

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _kind(self) -> ResolutionContractReferenceV1:
        if self.identity.kind != "ResolutionContract":
            raise ValueError("investigation must reference a ResolutionContract")
        return self


class InvestigationBindingV1(_ContractModel):
    contract: ResolutionContractReferenceV1
    hypothesis: ClaimVersionReferenceV1
    window: BoundObservationWindowV1


class ResolutionContractsRequestV1(_ContractModel):
    hypothesis: ClaimVersionReferenceV1
    at: AcceptedCoordinate | None = None


class ResolutionContractViewV1(_ContractModel):
    reference: ResolutionContractReferenceV1
    contract: ResolutionContractV1


class ResolutionContractsResultV1(_ContractModel):
    coordinate: AcceptedCoordinate
    contracts: tuple[ResolutionContractViewV1, ...]
