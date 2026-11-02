"""Executor-owned changes inferred from a governed claims template.

These are never provider output or authored item data. Only expand_claims builds
this side channel; terminal item values remain inert identity material.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    AuthoringNoneClaimObject,
    ClaimDerivationBinding,
)
from cruxible_client.contracts.claim_evidence import AbsenceWitness
from cruxible_client.contracts.claims import LiteralClaimObject, SubjectClaimObject
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.shapes import claim_rationale
from cruxible_client.contracts.subject_kinds import SubjectKeyReference, SubjectKeyValue


class CoreClaimStatement(AuthoringClaimStatement):
    subject: SemanticAddress | SubjectKeyReference  # type: ignore[assignment]
    object: Annotated[
        LiteralClaimObject | SubjectClaimObject | AuthoringNoneClaimObject,
        Field(discriminator="kind"),
    ]


class _CoreChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rationale: str

    _rationale = field_validator("rationale")(claim_rationale)


class CoreClaimChange(_CoreChange):
    kind: Literal["claim"] = "claim"
    statement: CoreClaimStatement
    predecessor: str | None = None
    derivation: ClaimDerivationBinding | None = None


class CoreRetirementChange(_CoreChange):
    kind: Literal["retirement"] = "retirement"
    claim_id: str
    artifact_digest: str
    claim_type: str
    witness: AbsenceWitness


class CoreSubjectChange(_CoreChange):
    kind: Literal["subject"] = "subject"
    subject_kind: str
    key: dict[str, SubjectKeyValue]


class CoreReobservationChange(_CoreChange):
    kind: Literal["reobservation"] = "reobservation"
    claim_id: str
    artifact_digest: str
    value: object


CoreClaimsChange = Annotated[
    CoreClaimChange | CoreSubjectChange | CoreReobservationChange | CoreRetirementChange,
    Field(discriminator="kind"),
]
