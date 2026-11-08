"""Build accepted Procedures, and their bound execution plans, for tests.

A Procedure is one envelope (``cruxible-procedure-v4``) whose graph names its
dependencies. Its input, output and parameters are governed Contracts: a test
declares each with :func:`boundary_contract`, names it in the graph with
:func:`contract_pin`, and accepts its file (:func:`contract_files`) beside the
Procedure when the run checks real values.

A fixture may still spell a dependency as an exact pin (``role``, ``target``,
``artifact_digest``): the graph names its target, and the pin's digest is what
:func:`accepted_procedure`'s execution plan resolves it to.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from functools import cache
from hashlib import sha256
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.authored import (
    ProcedureLimits,
    ProcedureSpec,
    ProjectStep,
    lower_procedure_spec,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    derived_terminal_capability,
)
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
    validate_contract,
)

#: Wall clock for a test Procedure whose subject is not time, and its hard cap.
#: Run budgets of 1-10 s ran out on loaded CI runners (GitHub Actions runs
#: 38296876564 and 38327886843): a run is measured from admission through every
#: node on a shared host. A test about exhaustion injects a clock instead.
RUN_WALL_CLOCK = CanonicalDuration(microseconds=60_000_000)
MAX_WALL_CLOCK = CanonicalDuration(microseconds=120_000_000)


def procedure_limits(**overrides: Any) -> ProcedureLimits:
    """Authored caps; a test states only the limits it exercises."""

    return ProcedureLimits(**overrides)


#: Where authoring applies the Procedure template refusal (D-R37 h1).
TEMPLATE_REFUSAL_SITES = (
    "cruxible_client.contracts.authoring.inputs",
    "cruxible_client.authoring.procedures",
    "cruxible_core.authoring.procedure_spec",
)


@contextmanager
def executor_only_steps() -> Iterator[None]:
    """Author closed step kinds for an executor-only fixture; nothing else changes.

    Authoring refuses guard, halt, select, repeat, invoke, emit, project and the
    shape/join/aggregate/union/derive transforms for every Procedure (D-R37 h1).
    Their executor handlers stay until they are deleted post-v1 (h3). A module
    marked ``executor_only`` gets this for every test (``tests/conftest.py``); a
    module- or session-scoped fixture that builds such nodes enters it itself.
    Only the template refusal is suspended; every other authoring check runs.
    The patch is live, so world templates build such worlds fresh.
    """

    with pytest.MonkeyPatch.context() as patch:
        for name in TEMPLATE_REFUSAL_SITES:
            patch.setattr(importlib.import_module(name), "refuse_closed_steps", lambda spec: None)
        yield


# -- closed step kinds through the builder (R-ON4) ------------------------------------
#
# The SDK ``Sequence`` has no builder for the step kinds the template closes
# (project, guard, halt, invoke, select, repeat, emit and the closed transforms).
# An ``executor_only`` test that composes one with open steps appends it here.


def closed_step(
    plan: Any, step: Any, *, name: str | None = None, fields: tuple[str, ...] | None = None
) -> Any:
    """Append ``step`` to a ``Sequence``; a value step returns its ``Handle``."""

    from cruxible_client.authoring.procedures import Handle

    plan._steps.append(step)  # noqa: SLF001
    return None if name is None else Handle("step", step=name, fields=fields)


def project(
    plan: Any,
    name: str,
    *,
    fields: Any = None,
    literal: object = None,
    output: Mapping[str, Any] | None = None,
    leaf: bool = False,
    id: str | None = None,
    next: str | None = None,
) -> Any:
    step = ProjectStep.model_validate(
        dict(
            project=name,
            fields=dict(fields) if isinstance(fields, Mapping) else fields,
            literal=literal,
            output=None if output is None else dict(output),
            leaf=leaf,
            id=id,
            next=next,
        )
    )
    known = (
        tuple(output)
        if output is not None
        else tuple(fields)
        if isinstance(fields, Mapping)
        else None
    )
    return closed_step(plan, step, name=name, fields=known)


def emit(
    plan: Any, capture_contract: str, *, input: Any, result: Any, id: str | None = None
) -> None:
    from cruxible_client.contracts.procedures.authored import EmitStep

    closed_step(plan, EmitStep(emit=capture_contract, input=input, result=result, id=id))


def guard(
    plan: Any,
    predicate: Any,
    *,
    then: Any = None,
    else_: Any = None,
    id: str | None = None,
) -> None:
    from cruxible_client.contracts.procedures.authored import GuardStep

    data: dict[str, Any] = {"guard": predicate, "then": then, "id": id}
    if else_ is not None:
        data["else"] = else_
    closed_step(plan, GuardStep.model_validate(data))


def halt_step(
    plan: Any, reason: str | None, *, message: str | None = None, id: str | None = None
) -> None:
    from cruxible_client.contracts.procedures.authored import HaltStep

    closed_step(plan, HaltStep(halt=reason, message=message, id=id))


def invoke(
    plan: Any,
    procedure: str,
    *,
    input: Any,
    as_: str,
    params: Mapping[str, Any] | None = None,
    id: str | None = None,
    next: str | None = None,
) -> Any:
    from cruxible_client.contracts.procedures.authored import InvokeStep

    step = InvokeStep.model_validate(
        {
            "invoke": procedure,
            "input": input,
            "params": dict(params or {}),
            "as": as_,
            "id": id,
            "next": next,
        }
    )
    return closed_step(plan, step, name=as_)


def abort(code: str, message: str) -> Any:
    from cruxible_client.contracts.procedures.authored import AbortAction, AbortDetail

    return AbortAction(abort=AbortDetail(code=code, message=message))


def halt(reason: str | None, *, message: str | None = None, id: str | None = None) -> Any:
    from cruxible_client.contracts.procedures.authored import HaltAction

    return HaltAction(halt=reason, message=message, id=id)


def goto(step: str) -> Any:
    from cruxible_client.contracts.procedures.authored import GotoAction

    return GotoAction(goto=step)


def procedure_spec(
    procedure: str | None = None,
    *,
    base: Mapping[str, Any] | None = None,
    changes: Mapping[str, Any] | None = None,
    reference_text: bool = False,
    **overrides: Any,
) -> ProcedureSpec:
    """A typed authored Procedure, with a constant result unless steps are supplied.

    Keep defaults on the public spec model rather than copying its wire shape
    into each test. Invalid authored inputs belong in the refusal tests, which
    deliberately bypass this builder.
    """

    fields = {
        **dict(base or {}),
        **({} if procedure is None else {"procedure": procedure}),
        **overrides,
        **dict(changes or {}),
    }
    fields.setdefault("procedure", "fixture")
    fields.setdefault("steps", (ProjectStep(project="result", fields={}),))
    if reference_text:
        fields = refs(fields)
    return ProcedureSpec(**fields)


def procedure_spec_data(
    procedure: str | None = None, *, reference_text: bool = False, **overrides: Any
) -> dict[str, Any]:
    """The typed spec serialized for a test of a JSON authoring surface."""

    return procedure_spec(procedure, reference_text=reference_text, **overrides).model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )


@cache
def _lowered_default_spec() -> Any:
    """The immutable default envelope; callers merge overrides without mutating it."""

    return lower_procedure_spec(procedure_spec())


def procedure_hard_caps(
    preset: Literal["read", "execution", "provider", "source"] = "read", **overrides: Any
) -> ProcedureHardCaps:
    """Shared limits for reads, executor kernels, provider calls and source runs.

    A fixture selects its workload and states only limits that differ. Production
    lowering supplies any new fields; the presets keep historical fixture values.
    """

    presets = {
        "read": dict(
            max_provider_calls=0, max_capture_bytes=0, max_items=200, max_repeat_attempts=1
        ),
        "execution": dict(
            max_provider_calls=0, max_capture_bytes=0, max_items=100, max_repeat_attempts=3
        ),
        "provider": dict(
            max_provider_calls=4, max_capture_bytes=2048, max_items=20, max_repeat_attempts=2
        ),
        "source": dict(
            max_provider_calls=4, max_capture_bytes=131072, max_items=200, max_repeat_attempts=1
        ),
    }
    return ProcedureHardCaps(
        **{
            **_lowered_default_spec().definition["hard_caps"],
            "max_wall_clock": MAX_WALL_CLOCK,
            **presets[preset],
            **overrides,
        }
    )


def procedure_budget(**overrides: Any) -> ProcedureBudget:
    """A test run's operating budget, independent of the Procedure's caps."""

    return ProcedureBudget(
        **{
            "wall_clock": RUN_WALL_CLOCK,
            "max_provider_calls": 0,
            "max_capture_bytes": 0,
            **overrides,
        }
    )


def procedure_definition(
    name: str | None = None, *, base: ProcedureDefinition | None = None, **overrides: Any
) -> ProcedureDefinition:
    """A graph fixture with its envelope supplied by a lowered typed spec.

    Executor and graph-law tests need exact typed nodes, including deliberately
    invalid topology and authority declarations. Those override the lowered
    graph here without decompiling/re-lowering them (which could repair the
    subject of a refusal test or change a digest pin).
    """

    name = name if name is not None else base.name if base is not None else "fixture"
    fields = {
        **_lowered_default_spec().definition,
        "name": name,
        "contract_in": ArtifactNameRef(
            target=ArtifactIdentity(kind="Contract", name=f"{name}.input/v1")
        ),
        "contract_out": ArtifactNameRef(
            target=ArtifactIdentity(kind="Contract", name=f"{name}.output/v1")
        ),
        "hard_caps": procedure_hard_caps(),
        **({} if base is None else base.model_dump(mode="python", by_alias=True)),
        **overrides,
    }
    if "terminal_capability" not in overrides and (base is None or "nodes" in overrides):
        fields["terminal_capability"] = derived_terminal_capability(fields["nodes"])
    return ProcedureDefinition.model_validate(fields)


def boundary_contract(
    name: str, fields: Mapping[str, Any] | None = None, *nested: Contract
) -> Contract:
    """Version 1 of one governed Contract a test Procedure's boundary names.

    Each nested ``name@N`` it spells is pinned to the matching ``nested`` Contract.
    """

    from cruxible_client.contracts.shapes import pin_contract_refs

    declared = validate_contract({"name": name, "version": 1, "fields": dict(fields or {})})
    by_key = {(item.name, item.version): item for item in nested}

    def pin(ref: Any) -> Any:
        target = by_key[(ref.name, ref.version)]
        return ref.model_copy(update={"digest": contract_digest(target).tagged})

    pinned = pin_contract_refs(declared, pin)
    declare_boundaries(pinned, *nested)
    return pinned


def contract_pin(role: str, contract: Contract) -> ArtifactPin:
    """A fixture pin to one governed Contract (``named`` spells it in a graph)."""

    return ArtifactPin(
        role=role, target=contract.identity, artifact_digest=contract_digest(contract).tagged
    )


def contract_files(*contracts: Contract) -> dict[str, bytes]:
    """The accepted-tree files of governed Contracts."""

    return {
        contract_path(contract.name, contract.version): render_contract(contract)
        for contract in contracts
    }


#: Every boundary Contract a test fixture declared, by identity: what
#: :func:`procedure_members` accepts beside a Procedure that names it.
_DECLARED: dict[ArtifactIdentity, Contract] = {}


def declare_boundaries(*contracts: Contract) -> tuple[Contract, ...]:
    """Record fixture boundary Contracts so :func:`procedure_members` can accept them."""

    additions: dict[ArtifactIdentity, Contract] = {}
    for contract in contracts:
        existing = additions.get(contract.identity, _DECLARED.get(contract.identity))
        if existing is not None and existing != contract:
            raise ValueError(
                f"{contract.identity.qualified} is already declared differently; "
                "use a unique test Contract name"
            )
        additions[contract.identity] = contract
    _DECLARED.update(additions)
    return contracts


def procedure_members(procedure: Any) -> dict[str, bytes]:
    """A Procedure's tree file and each declared governed Contract its boundaries name."""

    from cruxible_client.contracts.procedures.artifacts import render_procedure
    from cruxible_core.procedures.reference_binding import authored_procedure

    artifact = (
        procedure if isinstance(procedure, ProcedureArtifact) else authored_procedure(procedure)
    )
    definition = artifact.definition
    named_boundaries = [definition.contract_in, definition.contract_out]
    if definition.parameter_contract is not None:
        named_boundaries.append(definition.parameter_contract)
    declared: dict[ArtifactIdentity, Contract] = {}
    pending = [ref.target for ref in named_boundaries]
    while pending:
        identity = pending.pop()
        if identity in declared or identity not in _DECLARED:
            continue
        declared[identity] = _DECLARED[identity]
        pending.extend(ref.identity for ref in declared[identity].refs())
    return {
        procedure_path(artifact.identity.name): render_procedure(artifact),
        **contract_files(*declared.values()),
    }


def contract_resolver(*contracts: Contract) -> Any:
    """A Contract resolver over exactly these Contracts (a pin's digest must match)."""

    by_key = {(contract.name, contract.version): contract for contract in contracts}

    def resolve(ref: Any) -> Contract | None:
        found = by_key.get((ref.name, ref.version))
        if found is None or (
            ref.digest is not None and contract_digest(found).tagged != ref.digest
        ):
            return None
        return found

    return resolve


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
    data["graph_format"] = 8
    return ProcedureDefinition.model_validate(data)


def procedure_artifact(
    definition: ProcedureDefinition | Mapping[str, Any],
    *,
    activation_policy: str = "abort",
    lifecycle: ArtifactLifecycle | None = None,
    pins: dict[str, str] | None = None,
) -> ProcedureArtifact:
    """One envelope over the definition (its boundary Contracts are governed)."""

    resolved = named_definition(definition, pins=pins)
    return ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=resolved.name),
        definition=resolved,
        definition_digest=compute_procedure_definition_digest(resolved).tagged,
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
        declared = _DECLARED.get(identity)
        if declared is not None and identity.qualified not in digests:
            return declared, contract_digest(declared).tagged
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
    extra_pins: Iterable[ArtifactPin] = (),
    activation_policy: str = "abort",
    lifecycle: ArtifactLifecycle | None = None,
    bound: bool = True,
) -> AcceptedProcedure:
    """The accepted Procedure, bound (by default) into its execution plan."""

    pins: dict[str, str] = {}
    procedure = procedure_artifact(
        definition,
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

    if isinstance(value, BaseModel):
        return refs(value.model_dump(mode="json", by_alias=True, exclude_unset=True))
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
    "procedure_spec",
    "procedure_spec_data",
    "procedure_limits",
    "procedure_hard_caps",
    "procedure_budget",
    "procedure_definition",
    "boundary_contract",
    "contract_files",
    "contract_pin",
    "contract_resolver",
    "declare_boundaries",
    "procedure_members",
    "ref",
    "refs",
    "spec_input",
    "spec_payload",
    "accepted_artifact",
    "accepted_procedure",
    "bind_fixture",
    "bind_to_build",
    "fixture_digest",
    "named",
    "named_definition",
    "procedure_artifact",
    "revalidated",
]
