"""Build accepted Procedures, and their bound execution plans, for tests.

A Procedure is one envelope (``cruxible-procedure-v3``) whose graph names its
dependencies and carries every Contract it names. Tests that only care about
graph behavior name Contracts loosely: each named Contract becomes a carried
Contract -- permissive unless a schema is supplied -- so the envelope law holds
without per-test bookkeeping.

A fixture may still spell a dependency as an exact pin (``role``, ``target``,
``artifact_digest``): the graph names its target, and the pin's digest is what
:func:`accepted_procedure`'s execution plan resolves it to.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from hashlib import sha256
from typing import Any

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import ProcedureDefinition, iter_references

PERMISSIVE_CONTRACT = ContractSchema(fields={}, allow_extra=True)

#: Wall clock for a test Procedure whose subject is not time, and its hard cap.
#: Run budgets of 1-10 s ran out on loaded CI runners (GitHub Actions runs
#: 38296876564 and 38327886843): a run is measured from admission through every
#: node on a shared host. A test about exhaustion injects a clock instead.
RUN_WALL_CLOCK = CanonicalDuration(microseconds=60_000_000)
MAX_WALL_CLOCK = CanonicalDuration(microseconds=120_000_000)


def owned_contract(name: str, schema: ContractSchema | None = None) -> ProcedureOwnedContract:
    return ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name=name),
        schema=PERMISSIVE_CONTRACT if schema is None else schema,
    )


def owned_pin(role: str, contract: ProcedureOwnedContract) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=contract.identity,
        artifact_digest=procedure_owned_contract_digest(contract).tagged,
    )


def named(pin: Any) -> Any:
    """The graph reference a fixture pin names (``None`` stays ``None``)."""

    if pin is None or isinstance(pin, ArtifactNameRef):
        return pin
    return ArtifactNameRef(target=pin.target)


def _named_graph(value: Any, *, pins: dict[str, str]) -> Any:
    """``value`` with every exact pin spelled as the name it targets.

    The pins' digests are collected into ``pins`` by qualified identity.
    """

    if isinstance(value, ArtifactPin):
        pins.setdefault(value.target.qualified, value.artifact_digest)
        return {"target": value.target.model_dump(mode="json")}
    if isinstance(value, ArtifactNameRef):
        return value.model_dump(mode="json")
    if isinstance(value, ProcedureDefinition):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        target = value.get("target")
        if set(value) == {"role", "target", "artifact_digest"} and isinstance(target, dict):
            identity = ArtifactIdentity.model_validate(target)
            pins.setdefault(identity.qualified, str(value["artifact_digest"]))
            return {"target": target}
        return {key: _named_graph(item, pins=pins) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_named_graph(item, pins=pins) for item in value]
    if hasattr(value, "model_dump"):
        return _named_graph(value.model_dump(mode="json", by_alias=True), pins=pins)
    return value


def named_definition(
    definition: ProcedureDefinition | Mapping[str, Any],
    *,
    pins: dict[str, str] | None = None,
) -> ProcedureDefinition:
    """The one graph format, from a fixture that may spell dependencies as pins."""

    if isinstance(definition, ProcedureDefinition):
        return definition
    data = _named_graph(dict(definition), pins={} if pins is None else pins)
    data["graph_format"] = 7
    return ProcedureDefinition.model_validate(data)


def carried_definition(
    definition: ProcedureDefinition | Mapping[str, Any],
    *,
    contracts: Mapping[str, ContractSchema] | None = None,
    pins: dict[str, str] | None = None,
) -> tuple[ProcedureDefinition, tuple[ProcedureOwnedContract, ...]]:
    """The named definition and a carried Contract for every Contract it names."""

    resolved = named_definition(definition, pins=pins)
    names = sorted(
        {
            reference.target.name
            for reference in iter_references(resolved)
            if reference.target.kind == "Contract"
        }
    )
    owned = tuple(
        sorted(
            (owned_contract(name, (contracts or {}).get(name)) for name in names),
            key=lambda item: canonical_bytes(item.model_dump(mode="json", by_alias=True)),
        )
    )
    return resolved, owned


def procedure_artifact(
    definition: ProcedureDefinition | Mapping[str, Any],
    *,
    contracts: Mapping[str, ContractSchema] | None = None,
    activation_policy: str = "abort",
    lifecycle: ArtifactLifecycle | None = None,
    pins: dict[str, str] | None = None,
) -> ProcedureArtifact:
    """One envelope over the definition, its Contracts carried."""

    resolved, owned = carried_definition(definition, contracts=contracts, pins=pins)
    return ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=resolved.name),
        definition=resolved,
        definition_digest=compute_procedure_definition_digest(resolved).tagged,
        owned_contracts=owned,
        activation_policy=activation_policy,
        lifecycle=lifecycle or ArtifactLifecycle(),
    )


def revalidated(procedure: ProcedureArtifact) -> ProcedureArtifact:
    """``procedure`` (typically a ``model_copy``) through the envelope law again."""

    return ProcedureArtifact.model_validate(procedure.model_dump(mode="json", by_alias=True))


def accepted_artifact(procedure: ProcedureArtifact) -> AcceptedProcedure:
    return AcceptedProcedure(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def fixture_digest(identity: ArtifactIdentity) -> str:
    """A stand-in exact digest for a dependency a fixture names without pinning."""

    return "sha256:" + sha256(identity.qualified.encode("utf-8")).hexdigest()


def bind_fixture(
    accepted: AcceptedProcedure,
    *,
    pins: Mapping[str, str] | None = None,
    extra_pins: Iterable[ArtifactPin | ArtifactNameRef] = (),
    lookup: Any = None,
    bindings: Mapping[str, Any] | None = None,
    contracts: Any = None,
    partial: bool = True,
) -> AcceptedProcedure:
    """``accepted`` bound into its execution plan at the fixture's pins.

    Each dependency resolves to the digest a fixture pin gave it, else to a
    stand-in; ``lookup`` overrides either for the identities it answers.
    """

    from cruxible_core.procedures.reference_binding import bind_procedure_references

    digests = dict(pins or {})
    for pin in extra_pins:
        if isinstance(pin, ArtifactPin):
            digests.setdefault(pin.target.qualified, pin.artifact_digest)

    def resolve(identity: ArtifactIdentity) -> tuple[Any, str] | None:
        if lookup is not None:
            found = lookup(identity)
            if found is not None:
                return found
        return None, digests.get(identity.qualified, fixture_digest(identity))

    return bind_procedure_references(
        accepted,
        lookup=resolve,
        bindings=bindings,
        contracts=contracts,
        partial=partial,
    )


def bind_to_build(accepted: AcceptedProcedure, build: Any, operation: str) -> AcceptedProcedure:
    """``accepted`` bound with every slot on ``build``'s ``operation``."""

    from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
    from cruxible_client.contracts.providers import provider_digest

    digest = provider_digest(build).tagged
    binding = LineBinding(build=binding_pin(build.identity, digest), operation=operation)
    return bind_fixture(
        accepted,
        lookup=lambda identity: (build, digest) if identity == build.identity else None,
        bindings={slot: binding for slot in accepted.procedure.definition.slots},
    )


def accepted_procedure(
    definition: ProcedureDefinition | Mapping[str, Any],
    *,
    contracts: Mapping[str, ContractSchema] | None = None,
    extra_pins: Iterable[ArtifactPin] = (),
    activation_policy: str = "abort",
    lifecycle: ArtifactLifecycle | None = None,
    bound: bool = True,
) -> AcceptedProcedure:
    """The accepted Procedure, bound (by default) into its execution plan."""

    pins: dict[str, str] = {}
    procedure = procedure_artifact(
        definition,
        contracts=contracts,
        activation_policy=activation_policy,
        lifecycle=lifecycle,
        pins=pins,
    )
    accepted = accepted_artifact(procedure)
    if not bound:
        return accepted
    return bind_fixture(accepted, pins=pins, extra_pins=extra_pins)


def ref(text: str) -> dict[str, Any]:
    """A typed reference (``{"$ref": ...}``) spelled the way the stored graph spells it.

    Tests write ``ref("$steps.rows.result")``; the authored form is the typed
    object, never the string.
    """

    from cruxible_client.contracts.refs import ref_from_graph_text

    found = ref_from_graph_text(text)
    if found is None:
        raise ValueError(f"{text!r} is not a reference")
    return found.model_dump(mode="json", by_alias=True)


_SHORT_STEP_REF = re.compile(r"^\$[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")


def refs(value: Any) -> Any:
    """``value`` with every ``$input``/``$steps``/``$item``/``$params`` string as a typed ref.

    A test may also spell a step reference ``$<step>.<path>``: it becomes the
    typed step reference. The authored form has no strings; this is only how
    tests write it briefly.
    """

    if isinstance(value, str) and value.startswith(("$input", "$steps.", "$item", "$params.")):
        return ref(value)
    if isinstance(value, str) and _SHORT_STEP_REF.fullmatch(value):
        return ref("$steps." + value[1:])
    if isinstance(value, dict):
        return {key: refs(member) for key, member in value.items()}
    if isinstance(value, list | tuple):
        return [refs(member) for member in value]
    return value


def spec_input(spec: Mapping[str, Any]) -> Any:
    """The one Procedure authoring input for a spec (``{kind: procedure, **spec}``)."""

    from cruxible_client.contracts.authoring.inputs import ProcedureInput

    return ProcedureInput.model_validate({"kind": "procedure", **spec})


def spec_payload(spec: Mapping[str, Any]) -> Any:
    """The lowered authoring payload a spec submits through every surface."""

    from cruxible_client.contracts.authoring.inputs import lower_authoring_input

    return lower_authoring_input(spec_input(spec))


__all__ = [
    "PERMISSIVE_CONTRACT",
    "ref",
    "refs",
    "spec_input",
    "spec_payload",
    "accepted_artifact",
    "accepted_procedure",
    "bind_fixture",
    "bind_to_build",
    "carried_definition",
    "fixture_digest",
    "named",
    "named_definition",
    "owned_contract",
    "owned_pin",
    "procedure_artifact",
    "revalidated",
]
