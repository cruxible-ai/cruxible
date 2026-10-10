"""What one `propose_change_set` terminal item is allowed to say.

A Procedure's `candidate_templates` are canonical objects the graph resolves
at run time. Once resolved, each one has to be exactly one of the shapes
below before it can become a change-set member; anything else refuses the
terminal, typed to the item, before lowering runs.

The item names a Claim STATEMENT and the author's rationale, and nothing
about its evidence. Evidence is never caller-supplied: the daemon attaches the
produced Capture that sits in the item's own dependency closure (or, when the
closure produced none, the one Capture the run was admitted, such as a Line's
trigger input), so a computed interpretation cites exactly the observation the
run made or consumed and cannot borrow, invent, or omit one.

A Subject item creates a Subject by its declared kind and key values, never by
id (identity D1): core assigns the id, and a key a live Subject already holds
names that Subject instead (the item is reported ``reused``). A Claim item of
the same terminal names such a Subject -- or any Subject the run knows only by
its key -- with a ``SubjectKeyReference`` in place of an address (v3).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimObject,
    AuthoringClaimStatement,
    AuthoringExistingClaimDisposition,
    ClaimDerivationBinding,
    ExistingCaptureCitationSource,
    SelfSourceBody,
)
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subject_kinds import (
    SubjectKeyReference,
    SubjectKeyValue,
    key_value_problem,
)


class _StrictProposalItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureClaimProposalItemV1(_StrictProposalItemModel):
    """One Claim a Procedure proposes: its statement, its rationale, its lineage."""

    tag: Literal["playbill-procedure-claim-proposal-item-v1"] = (
        "playbill-procedure-claim-proposal-item-v1"
    )
    statement: AuthoringClaimStatement
    rationale: str
    revises: str | None = None

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("proposal item rationale must not be empty")
        return value

    @field_validator("revises")
    @classmethod
    def _revises(cls, value: str | None) -> str | None:
        if value is not None:
            claim_path(value)
        return value


class ProcedureClaimProposalItem(ProcedureClaimProposalItemV1):
    """Resolved source candidate; evidence and derivation are bound by the executor."""

    tag: Literal["playbill-procedure-claim-proposal-item-v2"] = (
        "playbill-procedure-claim-proposal-item-v2"  # type: ignore[assignment]
    )
    source: ExistingCaptureCitationSource | SelfSourceBody
    citation_role: Literal["evidence", "copy"] | None = None
    derivation: ClaimDerivationBinding | None = None
    existing_claim_dispositions: tuple[AuthoringExistingClaimDisposition, ...] = ()


class SubjectKeyClaimObject(_StrictProposalItemModel):
    """A Subject-valued Claim object named by its kind's key, not its id."""

    kind: Literal["subject_key"]
    subject: SubjectKeyReference


ProcedureClaimObject = Annotated[
    AuthoringClaimObject | SubjectKeyClaimObject, Field(discriminator="kind")
]


class ProcedureClaimStatement(AuthoringClaimStatement):
    """A Claim statement whose Subjects a run may name by key (``SubjectKeyReference``).

    Delivery resolves each key reference -- to the Subject an item of the same
    terminal creates or reuses with that key, else to the one live Subject
    holding it -- and lowers the plain statement.
    """

    tag: Literal["playbill-procedure-claim-statement-v1"] = "playbill-procedure-claim-statement-v1"  # type: ignore[assignment]
    subject: SemanticAddress | SubjectKeyReference  # type: ignore[assignment]
    object: ProcedureClaimObject  # type: ignore[assignment]


class ProcedureKeyedClaimProposalItem(ProcedureClaimProposalItem):
    """A v2 Claim item whose statement may name Subjects by key (identity D1).

    ``source`` may be omitted: the Claim then cites the one Capture its own
    closure reached, as a v1 item does.
    """

    tag: Literal["playbill-procedure-claim-proposal-item-v3"] = (
        "playbill-procedure-claim-proposal-item-v3"  # type: ignore[assignment]
    )
    statement: ProcedureClaimStatement
    source: ExistingCaptureCitationSource | SelfSourceBody | None = None  # type: ignore[assignment]


class ProcedureSubjectProposalItem(_StrictProposalItemModel):
    """One Subject a Procedure creates: its declared kind and key values, never an id.

    ``key`` maps each key ClaimType of the kind (by predicate, or by its last
    name under the kind) to its value; it is empty for a minted kind. A key one
    live Subject holds names it, and the item creates nothing (``reused``);
    ``create_new`` asks for a further holder of a ``hint`` kind's key. The key
    Claims cite ``source``, or -- like a v1 Claim item -- the one Capture the
    item's own closure reached.
    """

    tag: Literal["playbill-procedure-subject-proposal-item-v1"] = (
        "playbill-procedure-subject-proposal-item-v1"
    )
    subject_kind: str
    key: dict[str, SubjectKeyValue] = Field(default_factory=dict)
    rationale: str
    create_new: bool = False
    source: ExistingCaptureCitationSource | SelfSourceBody | None = None

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("proposal item rationale must not be empty")
        return value

    @field_validator("key")
    @classmethod
    def _key(cls, value: dict[str, SubjectKeyValue]) -> dict[str, SubjectKeyValue]:
        for item in value.values():
            problem = key_value_problem(item)
            if problem is not None:
                raise ValueError(problem)
        return value


#: Every shape a resolved `propose_change_set` template may take, by tag.
ProcedureProposalItemAny = (
    ProcedureClaimProposalItemV1
    | ProcedureClaimProposalItem
    | ProcedureKeyedClaimProposalItem
    | ProcedureSubjectProposalItem
)

PROPOSAL_ITEM_MODELS: dict[str, type[ProcedureProposalItemAny]] = {
    "playbill-procedure-claim-proposal-item-v1": ProcedureClaimProposalItemV1,
    "playbill-procedure-claim-proposal-item-v2": ProcedureClaimProposalItem,
    "playbill-procedure-claim-proposal-item-v3": ProcedureKeyedClaimProposalItem,
    "playbill-procedure-subject-proposal-item-v1": ProcedureSubjectProposalItem,
}


__all__ = [
    "PROPOSAL_ITEM_MODELS",
    "ProcedureClaimObject",
    "ProcedureClaimProposalItem",
    "ProcedureClaimProposalItemV1",
    "ProcedureClaimStatement",
    "ProcedureKeyedClaimProposalItem",
    "ProcedureProposalItemAny",
    "ProcedureSubjectProposalItem",
    "SubjectKeyClaimObject",
]
