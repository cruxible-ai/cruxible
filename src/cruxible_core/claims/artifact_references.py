"""Where each artifact family holds another artifact's digest, field by field.

Moving a definition moves the digests that name it. Rewriting every string that
equals an old digest would also rewrite literal data that merely holds one (a
schema constant, a claimed value), so re-pinning reads and writes only the
fields listed here. A family absent from the table has no automatic re-pinning.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.procedures.artifacts import (
    BlueprintArtifact,
    BlueprintOrigin,
    NamedBlueprintArtifact,
    NamedBlueprintOrigin,
    NamedProcedureArtifact,
    ProcedureArtifact,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import (
    CallNode,
    RepeatBodyNode,
    SourceNode,
    iter_pin_bindings,
)
from cruxible_client.contracts.procedures.source_program import (
    NamedProcedureSourceProgram,
    NamedSourceClaimType,
    NamedSourceProcedureBinding,
    NamedSourceProviderBinding,
    NamedSourceQueryBinding,
    NamedSourceSlotBinding,
    ProcedureSourceProgram,
    SourceClaimType,
    SourceProcedureBinding,
    SourceProviderBinding,
    SourceQueryBinding,
    SourceSlotBinding,
)

_PIN = ("*", "artifact_digest")
# A Claim's capture-contract pins are provenance -- the exact contract versions its
# evidence used -- so a Claim's references skip them and they never move.
_REQUIRED_CLAIM_PIN = ("*!role=capture-contract", "artifact_digest")

# Paths are field names, with "*" stepping into each list element. For the
# definition families every digest field is a reference; for Claims and
# Documents only the fields naming other artifacts are, never content digests.
REFERENCE_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "capture-contracts/": (
        ("components", "pins", *_PIN),
        ("components", "coordinate_schema_pins", *_PIN),
        ("components", "selector_schema_pins", *_PIN),
        ("components", "commitment_canonicalizer", "artifact_digest"),
        (
            "components",
            "replay_policy_digest",
        ),
        (
            "components",
            "provenance_rule_digest",
        ),
        (
            "components",
            "source_subject_mapping_digest",
        ),
        ("pins", *_PIN),
        ("coordinate_schema_pins", *_PIN),
        ("selector_schema_pins", *_PIN),
        ("commitment_canonicalizer", "artifact_digest"),
        ("retention_erasure_policy", "erasure_rule_digest"),
        ("replay_policy_digest",),
        ("provenance_rule_digest",),
        ("source_subject_mapping_digest",),
    ),
    "claim-types/": (
        ("pins", *_PIN),
        # Historical exact-digest evidence rules; v6 rules name contracts by
        # identity and need no re-pinning.
        ("evidence_admission_policy", "rules", "*", "capture_contract_digests", "*"),
        ("evidence_admission_policy", "rules", "*", "allowed_reducer_digests", "*"),
        ("admission_policy", "corroboration_requirements", "*", "query_definition_digest"),
    ),
    "query-definitions/": (("pins", *_PIN),),
    # A ProviderInterface is carried as its package registers it; its pins are
    # its only references (its other digests are its own content).
    "provider-interfaces/": (("pins", *_PIN),),
    # Only historical v1 policies carry pins; age-only v2 policies have no
    # digest references. Coherence and rule digests were values, not references.
    "source-acquisition-policies/": (("pins", *_PIN),),
    "claims/": (
        ("statement", "claim_type_digest"),
        ("pins", *_REQUIRED_CLAIM_PIN),
        ("backing", "input_claim_digests", "*"),
        ("backing", "reducer_digest"),
    ),
    "documents/": (("pins", "*", "target_digest"),),
    # A SubjectKind holds no digest: it names its key ClaimTypes by predicate.
    "subject-kinds/": (),
}


# Where a definition names another governed definition by identity. These never
# move -- an identity follows every version -- but whoever carries the naming
# definition must carry what it names.
IDENTITY_REFERENCE_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "claim-types/": (("evidence_admission_policy", "rules", "*", "capture_contracts", "*"),),
}


def referenced_identities(path: str, payload: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """The (kind, name) identities ``payload`` names by reference, never by digest."""

    for prefix, fields in IDENTITY_REFERENCE_FIELDS.items():
        if not path.startswith(prefix):
            continue
        for steps in fields:
            yield from _identities_at(payload, steps)
    if path.startswith("procedures/"):
        policy = payload.get("acquisition_policy")
        if isinstance(policy, str):
            yield "SourceAcquisitionPolicy", policy
    if path.startswith("subject-kinds/"):
        # A keyed SubjectKind names its key ClaimTypes by predicate.
        rule = payload.get("identity_rule")
        keys = rule.get("keys") if isinstance(rule, dict) else None
        for predicate in keys if isinstance(keys, list) else ():
            if isinstance(predicate, str):
                yield "ClaimType", predicate


def _identities_at(value: object, steps: tuple[str, ...]) -> Iterator[tuple[str, str]]:
    if not steps:
        target = value.get("target") if isinstance(value, dict) else None
        if isinstance(target, dict) and isinstance(target.get("kind"), str):
            name = target.get("name")
            if isinstance(name, str):
                yield target["kind"], name
        return
    head, rest = steps[0], steps[1:]
    if head == "*":
        for item in value if isinstance(value, list) else ():
            yield from _identities_at(item, rest)
    elif isinstance(value, dict) and head in value:
        yield from _identities_at(value[head], rest)


# Procedures and Blueprints hold their pins inside a graph whose digest their
# envelope carries, and a source program holds the same pins again as version
# strings: they move through their typed model (``_move_graph``), never a field
# table. A Procedure's Blueprint origin is provenance and never moves.
_GRAPH_FAMILIES = ("blueprints/", "procedures/")


def _graph_model(
    path: str, payload: Mapping[str, Any] | None = None
) -> type[ProcedureArtifact] | type[BlueprintArtifact] | None:
    if path.startswith("procedures/"):
        return (
            NamedProcedureArtifact
            if payload and payload.get("artifact_format") == "cruxible-procedure-v3"
            else ProcedureArtifact
        )
    if path.startswith("blueprints/"):
        return (
            NamedBlueprintArtifact
            if payload and payload.get("artifact_format") == "cruxible-blueprint-v2"
            else BlueprintArtifact
        )
    return None


def _source_versions(source: ProcedureSourceProgram | None) -> Iterator[str]:
    if source is None or isinstance(source, NamedProcedureSourceProgram):
        return
    for binding in source.bindings.values():
        if isinstance(binding, SourceProviderBinding):
            yield binding.provider_version
            yield binding.interface_version
        elif isinstance(binding, SourceSlotBinding):
            yield binding.interface_version
        else:
            yield binding.version
    for claim_type in source.claim_types.values():
        yield claim_type.version
    yield from source.capture_contracts.values()


class ProviderBuildMoveRefused(ValueError):
    """An occurrence cannot move to the target build: it implements none of the
    exact contracts the occurrence uses (``reason`` = ``contract_changed``)."""

    def __init__(self, message: str, *, reason: str = "contract_changed") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ProviderBuildMove:
    """Move every occurrence bound to one provider build onto another build.

    Unlike a digest remap this moves the pin's target identity too: the old and
    new builds are different artifacts (``Provider:<name>/<build key>``). Each
    occurrence moves to the new build's implementation of the same interface
    contract, and to the registration the new build binds for it.
    """

    source: ArtifactPin | ArtifactNameRef
    target: ArtifactPin
    #: Old implementation digest -> the new build's implementation of the same
    #: interface id and interface digest.
    implementations: Mapping[str, str] = field(default_factory=dict)
    #: Old registration artifact digest -> the registration pin the new build binds.
    interfaces: Mapping[str, ArtifactPin] = field(default_factory=dict)
    interface_names: Mapping[str, ArtifactNameRef] = field(default_factory=dict)


def _moved_occurrence(value: Any, move: ProviderBuildMove) -> dict[str, Any]:
    implementation = move.implementations.get(value.implementation_digest)
    interface = move.interfaces.get(value.interface.artifact_digest)
    if implementation is None or interface is None:
        raise ProviderBuildMoveRefused(
            f"{move.target.target.name} implements no exact contract of "
            f"{value.interface.target.name} that this occurrence uses"
        )
    return {
        "provider": move.target,
        "implementation_digest": implementation,
        "interface": interface,
    }


def _moved_binding(value: SourceProviderBinding, move: ProviderBuildMove) -> dict[str, Any]:
    implementation = move.implementations.get(value.implementation_digest)
    interface = move.interfaces.get(value.interface_version)
    if implementation is None or interface is None:
        raise ProviderBuildMoveRefused(
            f"{move.target.target.name} implements no exact contract of {value.interface} "
            "that this source binding uses"
        )
    return {
        "provider": move.target.target.name,
        "provider_version": move.target.artifact_digest,
        "implementation_digest": implementation,
        "interface": interface.target.name,
        "interface_version": interface.artifact_digest,
    }


def _build_move(pin: Any, builds: Mapping[str, ProviderBuildMove]) -> ProviderBuildMove | None:
    if not isinstance(pin, ArtifactPin | ArtifactNameRef):
        return None
    move = builds.get(pin.target.qualified)
    return (
        move
        if move is not None
        and (
            isinstance(pin, ArtifactNameRef)
            or isinstance(move.source, ArtifactNameRef)
            or move.source.artifact_digest == pin.artifact_digest
        )
        else None
    )


def _moved(
    value: Any,
    remap: Mapping[str, str],
    builds: Mapping[str, ProviderBuildMove] | None = None,
) -> Any:
    """``value`` with every pin and source version string moved through ``remap``,
    and every occurrence bound to a moved provider build moved to its new build."""

    def move(digest: str) -> str:
        return remap.get(digest, digest)

    if isinstance(value, ArtifactNameRef):
        selected = None if builds is None else builds.get(value.target.qualified)
        return value if selected is None else ArtifactNameRef(target=selected.target.target)
    if isinstance(value, ArtifactPin):
        moved = move(value.artifact_digest)
        return (
            value
            if moved == value.artifact_digest
            else value.model_copy(update={"artifact_digest": moved})
        )
    if isinstance(value, BlueprintOrigin | NamedBlueprintOrigin):
        return value
    if isinstance(value, NamedSourceProviderBinding):
        selected = None if builds is None else builds.get("Provider:" + value.provider)
        if selected is None:
            return value
        interface = selected.interface_names.get("ProviderInterface:" + value.interface)
        if interface is None:
            raise ProviderBuildMoveRefused("Target build does not serve the named source interface")
        return value.model_copy(
            update={"provider": selected.target.target.name, "interface": interface.target.name}
        )
    if isinstance(
        value,
        NamedSourceSlotBinding
        | NamedSourceQueryBinding
        | NamedSourceProcedureBinding
        | NamedSourceClaimType,
    ):
        return value
    updates: dict[str, Any] = {}
    if (
        builds
        and isinstance(value, SourceNode | CallNode | RepeatBodyNode)
        and isinstance(value.provider, ArtifactNameRef)
    ):
        selected = builds.get(value.provider.target.qualified)
        if selected is not None:
            assert value.interface is not None
            interface = selected.interface_names.get(value.interface.target.qualified)
            if interface is None:
                raise ProviderBuildMoveRefused("Target build does not serve the named interface")
            updates = {
                "provider": ArtifactNameRef(target=selected.target.target),
                "interface": interface,
            }
    if (
        builds
        and isinstance(value, SourceNode | CallNode | RepeatBodyNode)
        and not isinstance(value.provider, ArtifactNameRef)
    ):
        occurrence_move = _build_move(value.provider, builds)
        if occurrence_move is not None:
            updates = _moved_occurrence(value, occurrence_move)
    if isinstance(value, SourceProviderBinding):
        binding_move = (
            None
            if not builds
            else next(
                (
                    item
                    for item in builds.values()
                    if item.source.target.name == value.provider
                    and isinstance(item.source, ArtifactPin)
                    and item.source.artifact_digest == value.provider_version
                ),
                None,
            )
        )
        updates = (
            _moved_binding(value, binding_move)
            if binding_move is not None
            else {
                "provider_version": move(value.provider_version),
                "interface_version": move(value.interface_version),
            }
        )
    elif isinstance(value, SourceSlotBinding):
        updates = {"interface_version": move(value.interface_version)}
    elif isinstance(value, SourceQueryBinding | SourceProcedureBinding | SourceClaimType):
        updates = {"version": move(value.version)}
    elif isinstance(value, ProcedureSourceProgram) and not isinstance(
        value, NamedProcedureSourceProgram
    ):
        updates = {
            "capture_contracts": {
                name: move(digest) for name, digest in value.capture_contracts.items()
            }
        }
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            if name in updates:
                continue
            item = getattr(value, name)
            moved_item = _moved(item, remap, builds)
            if moved_item is not item:
                updates[name] = moved_item
        changed = {name: item for name, item in updates.items() if item != getattr(value, name)}
        return value.model_copy(update=changed) if changed else value
    if isinstance(value, tuple | list):
        items = [_moved(item, remap, builds) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return type(value)(items)
    if isinstance(value, dict):
        moved_values = {key: _moved(item, remap, builds) for key, item in value.items()}
        if all(moved_values[key] is value[key] for key in value):
            return value
        return moved_values
    return value


def _graph_digests(path: str, payload: Mapping[str, Any]) -> Iterator[str]:
    model = _graph_model(path, payload)
    assert model is not None
    graph = model.model_validate(payload)
    for pin in graph.pins:
        yield pin.artifact_digest
    yield from _source_versions(graph.definition.source)


_OCCURRENCE_PIN_ROLES = frozenset({"provider", "provider-interface"})


def _move_graph(
    path: str,
    payload: Mapping[str, Any],
    remap: Mapping[str, str],
    builds: Mapping[str, ProviderBuildMove] | None = None,
) -> dict[str, Any]:
    model = _graph_model(path, payload)
    assert model is not None
    graph = model.model_validate(payload)
    definition = _moved(graph.definition, remap, builds)
    if isinstance(graph, NamedProcedureArtifact | NamedBlueprintArtifact):
        moved = model.model_validate(
            {
                **graph.model_dump(mode="json", by_alias=True),
                "definition": definition.model_dump(mode="json", by_alias=True),
                "definition_digest": compute_procedure_definition_digest(definition).tagged,
            }
        )
        if isinstance(moved, NamedProcedureArtifact):
            from cruxible_client.contracts.procedures.wire_v3 import compact_procedure

            return compact_procedure(moved)
        return moved.model_dump(mode="json", by_alias=True)
    moved_pins = [_moved(pin, remap) for pin in graph.pins]
    if builds and definition is not graph.definition:
        # The Provider and interface pins are exactly the ones the occurrences
        # name: after a build move, read them again from the moved definition.
        moved_pins = [pin for pin in moved_pins if pin.role not in _OCCURRENCE_PIN_ROLES] + [
            pin
            for pin in iter_pin_bindings(definition)
            if isinstance(pin, ArtifactPin) and pin.role in _OCCURRENCE_PIN_ROLES
        ]
    unique = {
        (
            pin.role.encode("utf-8"),
            pin.target.qualified.encode("utf-8"),
            pin.artifact_digest.encode("ascii"),
        ): pin
        for pin in moved_pins
    }
    pins = tuple(unique[key] for key in sorted(unique))
    moved = model.model_validate(
        {
            **graph.model_dump(mode="json", by_alias=True),
            "definition": definition.model_dump(mode="json", by_alias=True),
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "pins": [pin.model_dump(mode="json") for pin in pins],
        }
    )
    result: dict[str, Any] = json.loads(json.dumps(moved.model_dump(mode="json", by_alias=True)))
    return result


def reference_fields(path: str) -> tuple[tuple[str, ...], ...] | None:
    """This path's reference fields, or None when its family has no table entry.

    Procedures and Blueprints have none: their references move only through
    ``move_references`` (their typed graph), never field by field.
    """

    for prefix, fields in REFERENCE_FIELDS.items():
        if path.startswith(prefix):
            return fields
    return None


def _at(value: object, steps: tuple[str, ...], remap: Mapping[str, str] | None) -> Iterator[str]:
    """Yield the digest at ``steps``; with ``remap``, rewrite it in place too."""

    if not steps:
        return
    head, rest = steps[0], steps[1:]
    if head.startswith("*!"):
        # "*!field=value": every list element except those whose field equals value.
        field, _, excluded = head[2:].partition("=")
        if isinstance(value, list):
            for item in value:
                if not (isinstance(item, dict) and item.get(field) == excluded):
                    yield from _at(item, rest, remap)
        return
    if head == "*":
        if isinstance(value, list):
            for index, item in enumerate(value):
                if not rest and isinstance(item, str):
                    yield item
                    if remap is not None:
                        value[index] = remap.get(item, item)
                else:
                    yield from _at(item, rest, remap)
        return
    if not isinstance(value, dict) or head not in value:
        return
    item = value[head]
    if not rest:
        if isinstance(item, str):
            yield item
            if remap is not None:
                value[head] = remap.get(item, item)
        return
    yield from _at(item, rest, remap)


def movable_references(path: str) -> bool:
    """Whether ``move_references`` can re-pin an artifact at ``path``: a family with a
    reference table, or a Procedure or Blueprint (through its typed graph)."""

    return path.startswith(_GRAPH_FAMILIES) or reference_fields(path) is not None


def referenced_digests(path: str, payload: Mapping[str, Any]) -> Iterator[str]:
    if path.startswith(_GRAPH_FAMILIES):
        yield from _graph_digests(path, payload)
        return
    for steps in reference_fields(path) or ():
        yield from _at(payload, steps, None)


def move_references(
    path: str,
    payload: Mapping[str, Any],
    remap: Mapping[str, str],
    builds: Mapping[str, ProviderBuildMove] | None = None,
) -> dict[str, Any]:
    """A copy of ``payload`` with its reference fields moved through ``remap``.

    ``builds`` (old build identity -> move) also moves a Procedure's occurrences
    bound to an old provider build onto the new build; it raises
    ``ProviderBuildMoveRefused`` when the new build lacks a contract one uses.
    """

    if path.startswith(_GRAPH_FAMILIES):
        return _move_graph(path, payload, remap, builds)
    fields = reference_fields(path)
    if fields is None:
        raise ValueError(f"{path} has no reference table; it cannot be re-pinned automatically")
    moved: dict[str, Any] = json.loads(json.dumps(payload))
    for steps in fields:
        for _ in _at(moved, steps, remap):
            pass
    return moved
