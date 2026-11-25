"""When a settle grant may follow a Line successor: an observation-only, pure build move.

A Line successor is a new Line digest, so a Mandate carried to it is
propose-only (ruling C3). Compiler revision 74 admits one exception
(maintainer ruling 3): a carried Mandate keeps ``grants="settle"`` when every
one of these holds, judged from the accepted parent tree and the candidate:

1. pure build move: the Line successor changes only slot bindings (same
   Procedure pin, parameters, acquisition and every other term);
2. non-widening per moved slot: the new operation serves every capability the
   old one served under the same ``in``/``out`` Contracts, its ``effect`` is no
   wider (``pure`` < ``external_read`` < ``external_mutation``), its entrypoint
   declares no endpoint the old one did not, and ``may_truncate`` does not turn
   on;
3. every ClaimType in the Mandate's scope is observation-only;
4. the Mandate creates no Subjects;
5. no live Mandate's condition query reads a predicate of that scope;
6. the carried Mandate keeps every term but its Line pin and predecessor.

The law (``proposals``) is the authority; ``build_moves`` asks the same
question to plan the carry and say why a grant demoted. Cost: the moved Line's
bindings, the Mandate's scope, and the reverse pins of each scoped ClaimType.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.claim_type_structure import is_observation_only_claim_type
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.procedure_mandates import ProcedureMandate
from cruxible_client.contracts.procedures.line_specs import LineSpec
from cruxible_client.contracts.provider_installation import SettleDemotion
from cruxible_client.contracts.provider_operations import OperationEffect
from cruxible_client.contracts.provider_registration import DeclaredOperation
from cruxible_client.contracts.providers import ProviderV2, parse_provider, provider_path
from cruxible_core.claims.closure import (
    ArtifactDependencyStateV1,
    DependencyIndexV1,
    SelectedReverseNeighbors,
    build_dependency_index,
    selected_reverse_neighbors,
)

_EFFECT_RANK: Mapping[OperationEffect, int] = {
    "pure": 0,
    "external_read": 1,
    "external_mutation": 2,
}

#: ``(identity) -> ((dependent state, its pin roles to identity), ...)``.
ReverseNeighbors = Callable[[str], Iterable[tuple[ArtifactDependencyStateV1, tuple[str, ...]]]]


def index_neighbors(index: DependencyIndexV1) -> ReverseNeighbors:
    """Reverse neighbours read from one maintained dependency index."""

    def neighbors(identity: str) -> Iterable[tuple[ArtifactDependencyStateV1, tuple[str, ...]]]:
        for path in sorted(index.sources_by_pinned_identity.get(identity, ())):
            state = index.states[path]
            yield (
                state,
                tuple(pin.role for pin in state.pins if pin.target.qualified == identity),
            )

    return neighbors


class LazyNeighbors:
    """The tree's reverse pins, opened on first use: the selected rows when the tree
    is instance-bound, else (a cold tree, as in a unit test) one index build."""

    def __init__(self, tree: Mapping[str, bytes]) -> None:
        self._tree = tree
        self._selected: SelectedReverseNeighbors | None = None
        self._read: ReverseNeighbors | None = None

    def __call__(
        self, identity: str
    ) -> Iterable[tuple[ArtifactDependencyStateV1, tuple[str, ...]]]:
        if self._read is None:
            self._selected = selected_reverse_neighbors(self._tree)
            self._read = (
                self._selected
                if self._selected is not None
                else index_neighbors(build_dependency_index(self._tree, by_name=True))
            )
        return self._read(identity)

    def close(self) -> None:
        if self._selected is not None:
            self._selected.close()
            self._selected = None
        self._read = None


def _operation(
    tree: Mapping[str, bytes], build: ArtifactPin, name: str
) -> tuple[ProviderV2, DeclaredOperation] | None:
    content = tree.get(provider_path(build.target.name))
    if content is None:
        return None
    provider = parse_provider(content, path=provider_path(build.target.name))
    if not isinstance(provider, ProviderV2):
        return None
    operation = next((item for item in provider.operations if item.name == name), None)
    return None if operation is None else (provider, operation)


def _endpoints(provider: ProviderV2, operation: DeclaredOperation) -> frozenset[str] | None:
    implementation = next(
        (
            item
            for item in provider.runtime_artifact.manifest.implementations
            if item.entrypoint == operation.entrypoint
        ),
        None,
    )
    return None if implementation is None else frozenset(implementation.declared_endpoints)


def build_move_refusal(
    old: LineSpec,
    new: LineSpec,
    *,
    before: Mapping[str, bytes],
    after: Mapping[str, bytes],
) -> SettleDemotion | None:
    """Criteria 1 and 2: the Line successor is a pure, non-widening build move."""

    unchanged = {"bindings", "lifecycle"}
    if (
        old.model_dump(mode="json", exclude=unchanged)
        != new.model_dump(mode="json", exclude=unchanged)
        or old.bindings.keys() != new.bindings.keys()
        or old.bindings == new.bindings
    ):
        return "line_changed"
    for slot, binding in new.bindings.items():
        previous = old.bindings[slot]
        if previous == binding:
            continue
        old_side = _operation(before, previous.build, previous.operation)
        new_side = _operation(after, binding.build, binding.operation)
        if old_side is None or new_side is None:
            return "capability_changed"
        (old_build, old_op), (new_build, new_op) = old_side, new_side
        if (
            not set(old_op.serves) <= set(new_op.serves)
            or old_op.in_ != new_op.in_
            or old_op.out != new_op.out
        ):
            return "capability_changed"
        if _EFFECT_RANK[new_op.effect] > _EFFECT_RANK[old_op.effect]:
            return "effect_widened"
        old_endpoints = _endpoints(old_build, old_op)
        new_endpoints = _endpoints(new_build, new_op)
        if old_endpoints is None or new_endpoints is None or not new_endpoints <= old_endpoints:
            return "endpoints_widened"
        if new_op.may_truncate and not old_op.may_truncate:
            return "may_truncate_widened"
    return None


def scope_refusal(
    mandate: ProcedureMandate, *, tree: Mapping[str, bytes], neighbors: ReverseNeighbors
) -> SettleDemotion | None:
    """Criteria 3, 4 and 5, read from the Mandate the parent tree accepted."""

    if mandate.subject_creation:
        return "subject_creation"
    scoped: list[ArtifactIdentity] = []
    for item in mandate.scope:
        path = claim_type_path(item.claim_type.target.name)
        content = tree.get(path)
        if content is None or not is_observation_only_claim_type(
            parse_claim_type(content, path=path)
        ):
            return "judgment_claim_type"
        scoped.append(item.claim_type.target)
    for claim_type in scoped:
        for query, _roles in neighbors(claim_type.qualified):
            if query.artifact_kind != "query-definition":
                continue
            for dependent, roles in neighbors(query.identity.qualified):
                if (
                    dependent.artifact_kind == "procedure-mandate"
                    and dependent.lifecycle.state == "live"
                    and "condition-query" in roles
                ):
                    return "condition_coupling"
    return None


def carried_terms_refusal(
    old: ProcedureMandate, new: ProcedureMandate, *, old_digest: str
) -> SettleDemotion | None:
    """Criterion 6: only the Line pin and the predecessor change."""

    expected = old.model_copy(
        update={
            "line": old.line.model_copy(update={"artifact_digest": new.line.artifact_digest}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=old_digest),
        }
    )
    return (
        None
        if new == expected and new.line.target == old.line.target
        else ("mandate_terms_changed")
    )


def observation_carry_refusal(
    *,
    old_mandate: ProcedureMandate,
    old_line: LineSpec,
    new_line: LineSpec,
    before: Mapping[str, bytes],
    after: Mapping[str, bytes],
    neighbors: ReverseNeighbors,
) -> SettleDemotion | None:
    """Why a settle grant on ``old_line`` may not follow it to ``new_line``, or None."""

    if old_mandate.grants != "settle":
        return "mandate_terms_changed"
    return build_move_refusal(old_line, new_line, before=before, after=after) or scope_refusal(
        old_mandate, tree=before, neighbors=neighbors
    )


def retained_scope(mandate: ProcedureMandate) -> tuple[str, ...]:
    """The ClaimTypes a retained settle grant keeps, by predicate."""

    return tuple(item.claim_type.target.name for item in mandate.scope)


__all__ = [
    "LazyNeighbors",
    "ReverseNeighbors",
    "SettleDemotion",
    "build_move_refusal",
    "carried_terms_refusal",
    "index_neighbors",
    "observation_carry_refusal",
    "retained_scope",
    "scope_refusal",
]
