"""The execution ports: what a run reads about its bound operations and acquisition.

The executor consumes these two ports only. Their implementations today are
adapters over the admitted Provider occurrence (the admission form of
:class:`~cruxible_core.procedures.reference_binding.BoundAcceptedProcedure`)
and over the accepted SourceAcquisitionPolicy. A Line-bound implementation can
replace either adapter without changing the executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from cruxible_client.contracts.acquisition_policies import (
    AcquisitionRuleAny,
    SourceAcquisitionPolicyAny,
    acquisition_policy_digest,
    acquisition_rule,
)
from cruxible_client.contracts.provider_execution import ProviderExternalOccurrencePlan
from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_DIGESTS

OperationEffect = Literal["pure", "external_read", "external_mutation", "none"]
#: Core built-in operations the executor serves specially, named by the port so
#: the executor never compares implementation identities itself.
BuiltinOperation = Literal["workspace.file"]


@dataclass(frozen=True, slots=True)
class BoundOperation:
    """One graph occurrence bound to one exact build and operation."""

    node_path: str
    build_digest: str
    operation: str
    in_digest: str | None
    out_digest: str | None
    effect: OperationEffect
    entrypoint: str
    builtin: BuiltinOperation | None = None


def bound_operation(occurrence: ProviderExternalOccurrencePlan) -> BoundOperation:
    """Adapter: the admitted Provider occurrence, read as a bound operation.

    Today an operation is named by its interface id and its signature by the
    interface digest; a Source occurrence's output is its CaptureContract.
    """

    return BoundOperation(
        node_path=occurrence.occurrence_path,
        build_digest=occurrence.provider_artifact_digest,
        operation=occurrence.interface_id,
        in_digest=occurrence.contract_input_digest,
        out_digest=(
            occurrence.capture_contract_digest
            if occurrence.occurrence_kind == "source"
            else occurrence.contract_output_digest
        ),
        effect=occurrence.effect_class,
        entrypoint=occurrence.local_execution.entrypoint,
        builtin=(
            "workspace.file"
            if occurrence.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS
            else None
        ),
    )


class AcquisitionRules(Protocol):
    """The acquisition rules one run admitted, addressed by node path and field."""

    @property
    def digest(self) -> str:
        """The digest the run admission recorded for these rules."""
        ...

    def rule_for(self, node_path: str, field: str) -> AcquisitionRuleAny | None:
        """The rule for one Capture-producing field of one node, if any serves it."""
        ...


@dataclass(frozen=True, slots=True)
class PolicyAcquisitionRules:
    """Adapter: an accepted SourceAcquisitionPolicy as the run's acquisition rules.

    A Source node today has one Capture output, named by its alias, and the
    policy is keyed by that alias; ``node_path`` does not narrow the lookup.
    """

    policy: SourceAcquisitionPolicyAny

    @property
    def digest(self) -> str:
        return acquisition_policy_digest(self.policy).tagged

    def rule_for(self, node_path: str, field: str) -> AcquisitionRuleAny | None:
        del node_path
        return acquisition_rule(self.policy, field)


def policy_acquisition_rules(
    policy: SourceAcquisitionPolicyAny | None,
) -> PolicyAcquisitionRules | None:
    return None if policy is None else PolicyAcquisitionRules(policy)


__all__ = [
    "AcquisitionRules",
    "BoundOperation",
    "BuiltinOperation",
    "OperationEffect",
    "PolicyAcquisitionRules",
    "bound_operation",
    "policy_acquisition_rules",
]
