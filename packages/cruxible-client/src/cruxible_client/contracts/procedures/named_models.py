"""Name-referenced graph format 7; format 6 remains a frozen read grammar."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field, model_validator

from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.procedures import models as old

NameBinding = ArtifactNameRef | old.ProcedurePinSlotRef


class NamedProcedurePinSlot(old.ProcedurePinSlot):
    tag: Literal["cruxible-procedure-slot-v2"] = "cruxible-procedure-slot-v2"  # type: ignore[assignment]
    pin_role: ClassVar[str] = "provider"  # type: ignore[misc]
    artifact_kind: ClassVar[str] = "Provider"  # type: ignore[misc]
    interface_digest: ClassVar[None] = None  # type: ignore[misc, assignment]
    interface: ArtifactNameRef


class NamedStateTapNode(old.StateTapNode):
    query: NameBinding  # type: ignore[assignment, misc]


class NamedSourceNode(old.SourceNode):
    capture_contract: NameBinding  # type: ignore[assignment, misc]
    provider: NameBinding  # type: ignore[assignment, misc]
    interface: ArtifactNameRef  # type: ignore[assignment, misc]
    interface_digest: ClassVar[None] = None  # type: ignore[assignment, misc]
    implementation_digest: ClassVar[None] = None  # type: ignore[assignment, misc]

    @model_validator(mode="after")
    def _provider_binding(self) -> NamedSourceNode:
        return self


class NamedExhaustTapNode(old.ExhaustTapNode):
    reducer_or_query: NameBinding  # type: ignore[assignment, misc]


class NamedCallNode(old.CallNode):
    provider: NameBinding  # type: ignore[assignment, misc]
    interface: ArtifactNameRef  # type: ignore[assignment, misc]
    contract_in: ClassVar[None] = None  # type: ignore[assignment, misc]
    contract_out: ClassVar[None] = None  # type: ignore[assignment, misc]
    effect_policy: NameBinding | None = None  # type: ignore[assignment, misc]
    interface_digest: ClassVar[None] = None  # type: ignore[assignment, misc]
    implementation_digest: ClassVar[None] = None  # type: ignore[assignment, misc]

    @model_validator(mode="after")
    def _provider_binding(self) -> NamedCallNode:
        return self


class NamedTransformNode(old.TransformNode):
    contract_in: NameBinding  # type: ignore[assignment, misc]
    contract_out: NameBinding  # type: ignore[assignment, misc]


class NamedProjectNode(old.ProjectNode):
    contract_out: NameBinding  # type: ignore[assignment, misc]


class NamedRepeatBodyNode(old.RepeatBodyNode):
    provider: NameBinding | None = None  # type: ignore[assignment, misc]
    interface: ArtifactNameRef | None = None  # type: ignore[assignment, misc]
    contract_in: NameBinding | None = None  # type: ignore[assignment, misc]
    contract_out: NameBinding | None = None  # type: ignore[assignment, misc]
    effect_policy: NameBinding | None = None  # type: ignore[assignment, misc]
    interface_digest: ClassVar[None] = None  # type: ignore[assignment, misc]
    implementation_digest: ClassVar[None] = None  # type: ignore[assignment, misc]

    @model_validator(mode="after")
    def _operation_shape(self) -> NamedRepeatBodyNode:
        if self.operation == "call":
            if self.provider is None or self.interface is None or self.transform_kind is not None:
                raise ValueError("repeat Calls name a provider and interface")
            if self.contract_in is not None or self.contract_out is not None:
                raise ValueError("Call I/O belongs to its interface")
            return self
        if self.provider is not None or self.interface is not None:
            raise ValueError("repeat transforms cannot declare a provider")
        if self.contract_in is None or self.contract_out is None:
            raise ValueError("repeat transforms require input and output contracts")
        if getattr(self.spec, "tag", None) != old._TRANSFORM_SPEC_TAGS.get(self.transform_kind):
            raise ValueError("repeat transform spec does not match its kind")
        return self


class NamedCaptureEgressNode(old.CaptureEgressNode):
    capture_contract: NameBinding  # type: ignore[assignment, misc]


class NamedProposeChangeSetNode(old.ProposeChangeSetNode):
    claim_types: tuple[ArtifactNameRef, ...] = ()  # type: ignore[assignment, misc]


class NamedClaimTapNode(old.ClaimTapNode):
    claim_type: ArtifactNameRef  # type: ignore[assignment, misc]


class NamedInvokeNode(old.InvokeNode):
    procedure: ArtifactNameRef  # type: ignore[assignment, misc]


class NamedSelectNode(old.SelectNode):
    contract_out: NameBinding  # type: ignore[assignment, misc]


class NamedReturnNode(old.ReturnNode):
    contract_out: NameBinding  # type: ignore[assignment, misc]


class NamedConstantNode(old.ConstantNode):
    contract_out: NameBinding  # type: ignore[assignment, misc]


class NamedSettleChangeSetNode(NamedProposeChangeSetNode, old.SettleChangeSetNode):
    kind: Literal["settle_change_set"] = "settle_change_set"  # type: ignore[assignment]


class NamedRepeatNode(old.RepeatNode):
    body: tuple[NamedRepeatBodyNode, ...]


NamedProcedureNode = Annotated[
    NamedInvokeNode
    | NamedClaimTapNode
    | NamedStateTapNode
    | NamedSourceNode
    | NamedExhaustTapNode
    | NamedCallNode
    | NamedTransformNode
    | old.GuardNode
    | NamedProjectNode
    | NamedRepeatNode
    | NamedCaptureEgressNode
    | old.InboxEgressNode
    | NamedProposeChangeSetNode
    | NamedSettleChangeSetNode
    | old.HaltNode
    | NamedSelectNode
    | NamedReturnNode
    | NamedConstantNode,
    Field(discriminator="kind"),
]


class NamedProcedureDefinition(old.ProcedureDefinition):
    graph_format: Literal[7] = 7  # type: ignore[assignment]
    contract_in: NameBinding  # type: ignore[assignment]
    contract_out: NameBinding  # type: ignore[assignment]
    parameter_contract: NameBinding | None = None  # type: ignore[assignment]
    nodes: tuple[NamedProcedureNode, ...]
    pin_slots: tuple[NamedProcedurePinSlot, ...] = ()
