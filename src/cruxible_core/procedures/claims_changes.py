"""Executor-owned changes inferred from a governed claims template.

These are never provider output or authored item data. Only expand_claims builds
this side channel; terminal item values remain inert identity material.
"""

from __future__ import annotations

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


class CoreCreatedSubject(BaseModel):
    """A Subject the same terminal creates, named by kind and the key its Subject item carries.

    A pair Subject's key holds its ends, so it has no ``subject-key-v1`` spelling:
    an item naming a pair this run creates -- a Claim about it, a Claim whose
    value it is, or the pair whose end it is -- names it by the exact key of the
    terminal's Subject item creating it, which delivery maps to that item's
    creation member (``subject-creation-v1``). ``key`` maps each key part to a
    literal, an existing end (a Subject Claim object) or another Subject the
    terminal creates. Never persisted: shared lowering lands it on a path.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["created"] = "created"
    subject_kind: str
    key: dict[str, CoreKeyPart] = Field(min_length=1)


#: One key part of a Subject item: a literal, an existing end, or an end the terminal creates.
CoreKeyPart = SubjectKeyValue | SubjectClaimObject | CoreCreatedSubject


class CoreClaimStatement(AuthoringClaimStatement):
    subject: SemanticAddress | SubjectKeyReference | CoreCreatedSubject  # type: ignore[assignment]
    object: Annotated[
        LiteralClaimObject | SubjectClaimObject | AuthoringNoneClaimObject | CoreCreatedSubject,
        Field(discriminator="kind"),
    ]  # type: ignore[assignment]


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
    #: A literal kind's key as the template spelled it; a pair kind's parts name
    #: each end as it exists, or as the Subject item creating it (ends first).
    key: dict[str, CoreKeyPart]


class CoreReobservationChange(_CoreChange):
    kind: Literal["reobservation"] = "reobservation"
    claim_id: str
    artifact_digest: str
    value: object


CoreCreatedSubject.model_rebuild()
CoreClaimStatement.model_rebuild()
CoreSubjectChange.model_rebuild()

CoreClaimsChange = Annotated[
    CoreClaimChange | CoreSubjectChange | CoreReobservationChange | CoreRetirementChange,
    Field(discriminator="kind"),
]
