"""The signature index: which live build operations serve one exact signature.

A slot is ``{in, out, effect}``. The typed projection keeps one row per live
build operation keyed by ``(in_digest, out_digest, effect)``
(``provider_operations``, maintained at accept: a changed build's rows are
replaced, so install and retire cost O(operations of the build)). Fitting a
slot is one indexed seek; it never enumerates builds.

Fitting happens only when a Line is accepted (or a direct run's ephemeral spec
is built): exactly one live operation must serve the signature. Zero refuses
``slot_unfilled``, several refuse ``slot_ambiguous``, each listing candidates.
Run admission never refits: it uses the binding the Line recorded.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.provider_operations import (
    LineBinding,
    OperationSignature,
    binding_pin,
)
from cruxible_client.contracts.providers import ProviderV2
from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
from cruxible_core.errors import RequestRefusedError

#: How many fitting candidates a refusal lists.
FIT_CANDIDATE_LIMIT = 20


def operation_index_rows(provider: Any) -> tuple[tuple[str, str, str, str, str], ...]:
    """The index rows one accepted build contributes: none unless it is live."""

    if not isinstance(provider, ProviderV2) or provider.lifecycle.state != "live":
        return ()
    rows = []
    for operation in provider.operations:
        if operation.in_.digest is None or operation.out.digest is None:
            continue
        rows.append(
            (
                provider.identity.qualified,
                operation.name,
                operation.in_.digest,
                operation.out.digest,
                operation.effect,
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class FittingOperation:
    build: str
    build_digest: str
    operation: str

    def binding(self) -> LineBinding:
        from cruxible_client.contracts.artifacts import parse_artifact_identity

        return LineBinding(
            build=binding_pin(parse_artifact_identity(self.build), self.build_digest),
            operation=self.operation,
        )

    @property
    def spelling(self) -> str:
        return f"{self.build.removeprefix('Provider:')}#{self.operation}"


def fitting_operations(
    connection: sqlite3.Connection,
    signature: OperationSignature,
    *,
    build: str | None = None,
    limit: int = FIT_CANDIDATE_LIMIT + 1,
) -> tuple[FittingOperation, ...]:
    """Live operations serving ``signature`` exactly, optionally within one build."""

    in_digest, out_digest, effect = signature.key()
    sql = (
        "SELECT o.source_identity,p.artifact_digest,o.operation "
        "FROM main.provider_operations o "
        "JOIN main.providers p ON p.identity=o.source_identity "
        "WHERE o.in_digest=? AND o.out_digest=? AND o.effect=? AND p.lifecycle='live'"
    )
    parameters: list[object] = [in_digest, out_digest, effect]
    if build is not None:
        sql += " AND o.source_identity=?"
        parameters.append(build)
    sql += " ORDER BY o.source_identity,o.operation LIMIT ?"
    parameters.append(limit)
    return tuple(
        FittingOperation(str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(sql, parameters)
    )


def exactly_one_fit(
    slot: str,
    signature: OperationSignature,
    candidates: Sequence[FittingOperation],
    *,
    within: str | None = None,
) -> FittingOperation:
    """The one candidate, or ``slot_unfilled`` / ``slot_ambiguous`` listing them."""

    if len(candidates) == 1:
        return candidates[0]
    scope = f" in {within}" if within is not None else ""
    if not candidates:
        raise RequestRefusedError(
            "cruxible.line.slot_unfilled",
            f"No live operation{scope} serves slot {slot!r} ({signature.spelling()}).",
            repair=HandEditRepair(
                hand_edit=HandEditInstruction(
                    target="bindings",
                    required_change=(
                        f"Install a build whose operation serves {signature.spelling()}, "
                        f"or bind {slot} to one explicitly."
                    ),
                )
            ),
        )
    listed = [item.spelling for item in candidates[:FIT_CANDIDATE_LIMIT]]
    raise RequestRefusedError(
        "cruxible.line.slot_ambiguous",
        f"Several live operations{scope} serve slot {slot!r} ({signature.spelling()}): "
        + ", ".join(listed)
        + (", ..." if len(candidates) > FIT_CANDIDATE_LIMIT else "")
        + ".",
        repair=HandEditRepair(
            hand_edit=HandEditInstruction(
                target="bindings",
                required_change=f"Bind {slot} to one of them: build#operation.",
            )
        ),
    )


def parse_binding_reference(reference: str) -> tuple[str, str | None]:
    """``provider[/build][#operation]`` as ``(provider identity name, operation)``."""

    name, _, operation = reference.removeprefix("Provider:").partition("#")
    return name, operation or None


def resolve_slot_bindings(
    connection: sqlite3.Connection,
    slots: Mapping[str, OperationSignature],
    requested: Mapping[str, str],
) -> dict[str, LineBinding]:
    """Every slot's exact ``{build, operation}``: as named, or fitted.

    A requested ``provider[/build][#operation]`` names the build (a bare provider
    name is its most recently registered live build) and, optionally, the
    operation; without one, the build's one operation fitting the slot. A slot
    left out is fitted across every live build: exactly one operation must serve
    its signature.
    """

    from cruxible_core.providers.builds import indexed_exact_provider, indexed_newest_provider

    unknown = sorted(set(requested) - set(slots))
    if unknown:
        raise RequestRefusedError(
            "cruxible.procedure.slot_undeclared",
            f"Unknown slots: {unknown}; declared slots: {sorted(slots)}.",
            repair=HandEditRepair(
                hand_edit=HandEditInstruction(
                    target="bindings",
                    required_change=f"Bind only declared slots: {sorted(slots)}.",
                )
            ),
        )
    resolved: dict[str, LineBinding] = {}
    for key, signature in sorted(slots.items()):
        reference = requested.get(key)
        if reference is None:
            resolved[key] = exactly_one_fit(
                key, signature, fitting_operations(connection, signature)
            ).binding()
            continue
        name, operation = parse_binding_reference(reference)
        row = (
            indexed_exact_provider(connection, "Provider:" + name)
            if "/" in name
            else indexed_newest_provider(connection, name)
        )
        if row is None or not row.live:
            raise RequestRefusedError(
                "cruxible.line.binding_unavailable",
                f"Slot {key!r} names {reference!r}, which is not an installed live build.",
                repair=HandEditRepair(
                    hand_edit=HandEditInstruction(
                        target="bindings",
                        required_change="Bind a live installed build (provider list).",
                    )
                ),
            )
        candidates = fitting_operations(connection, signature, build=row.identity)
        if operation is not None:
            candidates = tuple(item for item in candidates if item.operation == operation)
        resolved[key] = exactly_one_fit(
            key, signature, candidates, within=row.identity.removeprefix("Provider:")
        ).binding()
    return resolved


def line_slot_map(
    definition: Any,
    child: Callable[[ArtifactIdentity], Any],
) -> dict[str, OperationSignature]:
    """Every slot a Line over ``definition`` binds, invoked children's included.

    ``child`` reads the definition an invoke node names, at the authoring or
    admission coordinate.
    """

    from cruxible_client.contracts.procedures.line_specs import line_slot_signatures
    from cruxible_client.contracts.procedures.nodes import InvokeNode

    children: dict[str, Any] = {}

    def visit(graph: Any, prefix: str, depth: int) -> None:
        if depth > 16:
            raise ValueError("invoked Procedures nest too deeply to bind")
        for node in graph.nodes:
            if isinstance(node, InvokeNode):
                nested = child(node.procedure.target)
                if nested is None:
                    raise RequestRefusedError(
                        "cruxible.line.child_unavailable",
                        f"Invoked Procedure {node.procedure.target.qualified} is not accepted.",
                        repair=HandEditRepair(
                            hand_edit=HandEditInstruction(
                                target="procedure",
                                required_change="Accept the invoked Procedure first.",
                            )
                        ),
                    )
                children[prefix + node.node_id] = nested
                visit(nested, prefix + node.node_id + ".", depth + 1)

    visit(definition, "", 0)
    return line_slot_signatures(definition, child=children)


def child_bindings(bindings: Mapping[str, LineBinding], prefix: str) -> dict[str, LineBinding]:
    """The bindings of the slots one invoked child declares, keyed by its own slot names."""

    return {
        key[len(prefix) :]: value
        for key, value in bindings.items()
        if key.startswith(prefix) and "." not in key[len(prefix) :]
    }


__all__ = [
    "FIT_CANDIDATE_LIMIT",
    "FittingOperation",
    "exactly_one_fit",
    "fitting_operations",
    "operation_index_rows",
    "child_bindings",
    "line_slot_map",
    "parse_binding_reference",
    "resolve_slot_bindings",
]
