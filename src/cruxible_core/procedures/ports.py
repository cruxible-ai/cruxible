"""The execution ports: what a run reads about its bound operations and acquisition.

The executor consumes these two ports only. A bound operation is the admitted
occurrence's exact build and operation (the Line's binding for its slot); the
acquisition rules are the run's resolved rules (its Line's ``acquisition``
over its Procedure's ``acquisition_defaults``), keyed by capture output (``<alias>.<field>``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionRule,
    acquisition_rules_digest,
)
from cruxible_client.contracts.provider_execution import ProviderExternalOccurrencePlan
from cruxible_client.contracts.workspace_file import is_workspace_file_signature

OperationEffect = Literal["pure", "external_read", "external_mutation"]
#: Core built-in operations the executor serves specially, named by the port so
#: the executor never compares signatures itself.
BuiltinOperation = Literal["workspace.file"]


@dataclass(frozen=True, slots=True)
class BoundOperation:
    """One graph occurrence bound to one exact build and operation."""

    node_path: str
    slot: str
    build_digest: str
    operation: str
    in_digest: str | None
    out_digest: str | None
    effect: OperationEffect
    entrypoint: str
    builtin: BuiltinOperation | None = None


def bound_operation(occurrence: ProviderExternalOccurrencePlan) -> BoundOperation:
    """The admitted occurrence, read as a bound operation.

    Core recognizes its built-in ``workspace.file`` by exact signature.
    """

    return BoundOperation(
        node_path=occurrence.occurrence_path,
        slot=occurrence.slot,
        build_digest=occurrence.provider_artifact_digest,
        operation=occurrence.operation.name,
        in_digest=occurrence.operation.in_.digest,
        out_digest=occurrence.operation.out.digest,
        effect=occurrence.effect_class,
        entrypoint=occurrence.local_execution.entrypoint,
        builtin=(
            "workspace.file"
            if is_workspace_file_signature(occurrence.operation_signature_digest)
            else None
        ),
    )


class AcquisitionRules(Protocol):
    """The acquisition rules one run admitted, addressed by node path and field."""

    @property
    def digest(self) -> str:
        """The digest the run admission recorded for these rules."""
        ...

    def rule_for(self, node_path: str, field: str) -> AcquisitionRule | None:
        """The rule for one Capture-producing field of one node, if any serves it."""
        ...


@dataclass(frozen=True, slots=True)
class ResolvedAcquisitionRules:
    """A run's resolved rules, one per capture output (``<alias>.<field>``).

    A Source node today has one Capture output, named by its alias, and the
    rules are keyed by that alias; ``node_path`` does not narrow the lookup.
    """

    rules: Mapping[str, AcquisitionRule]

    @property
    def digest(self) -> str:
        return acquisition_rules_digest(self.rules)

    def rule_for(self, node_path: str, field: str) -> AcquisitionRule | None:
        del node_path
        return self.rules.get(field)


__all__ = [
    "AcquisitionRules",
    "BoundOperation",
    "BuiltinOperation",
    "OperationEffect",
    "ResolvedAcquisitionRules",
    "bound_operation",
]
