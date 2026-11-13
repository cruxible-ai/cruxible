"""The capability index: which live build operations serve one capability name.

Each live build operation contributes one row per capability name it lists in
``serves`` (``provider_operations``: build, operation, capability, effect),
written when the build is accepted and removed with it, so install and retire
cost O(operations of the build). A slot names one capability version; binding
seeks the rows for that capability's name and keeps the operations whose effect
is equal and that fit (note §2.2): what the slot sends fits what the operation
accepts, and what it returns fits what the slot expects. A seek reads only the
rows of that one name, so the work never grows with providers serving other
capabilities.

Binding happens when a Line is accepted (or a direct run's ephemeral spec is
built): exactly one live operation must serve the capability and fit, or the one
the Line names must. Refusals are note §2.6's (``slot_unfilled``,
``slot_ambiguous``, ``binding_unfit``, ``binding_undeclared``). Run admission
never chooses: it rechecks that the recorded binding still fits, memoized per
process by (capability digest, build digest, operation, kind digests).

The index is derived: an existing instance rebuilds it from its accepted tree
when the projection's storage schema version moves (cold start or format
change), never per write.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.capabilities import CapabilityLookup
from cruxible_client.contracts.capability_fit import (
    BindingFit,
    BindingUnfitReason,
    EffectUnequal,
    Fits,
    slot_ambiguous_message,
    slot_unfilled_message,
)
from cruxible_client.contracts.content_memo import ValueMemo
from cruxible_client.contracts.procedures.line_specs import (
    BindingSide,
    CaptureKindLookup,
    SlotFitter,
    SlotNeed,
    declared_kind,
    judge_slot_binding,
    operation_spelling,
    slot_fit,
)
from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
from cruxible_client.contracts.provider_registration import DeclaredOperation
from cruxible_client.contracts.providers import ProviderV2
from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair
from cruxible_core.errors import RequestRefusedError

#: How many candidates a refusal lists.
FIT_CANDIDATE_LIMIT = 20

#: Counted binding work, for the scale guard: index rows a seek returned, builds
#: read for their operations, and fits computed (memo misses).
BINDING_WORK = {"candidates": 0, "builds": 0, "fits": 0}


def operation_index_rows(provider: Any) -> tuple[tuple[str, str, str, str], ...]:
    """The index rows one accepted build contributes: one per (operation, capability
    name it serves), none unless the build is live."""

    if not isinstance(provider, ProviderV2) or provider.lifecycle.state != "live":
        return ()
    return tuple(
        (provider.identity.qualified, operation.name, capability, operation.effect)
        for operation in provider.operations
        for capability in operation.serves
    )


@dataclass(frozen=True)
class ServingOperation:
    """One index row: a live build's operation that names a capability in ``serves``."""

    build: str
    build_digest: str
    operation: str
    effect: str

    def binding(self) -> LineBinding:
        from cruxible_client.contracts.artifacts import parse_artifact_identity

        return LineBinding(
            build=binding_pin(parse_artifact_identity(self.build), self.build_digest),
            operation=self.operation,
        )

    @property
    def spelling(self) -> str:
        """``<provider>#<operation>``, as a refusal names an operation that does not fit."""

        return operation_spelling(self.build, self.operation)

    @property
    def build_spelling(self) -> str:
        """``<provider>/<build>#<operation>``, telling builds of one provider apart."""

        return f"{self.build.removeprefix('Provider:')}#{self.operation}"


def serving_operations(
    connection: sqlite3.Connection,
    capability: str,
    *,
    build: str | None = None,
) -> tuple[ServingOperation, ...]:
    """Live operations that name ``capability`` in ``serves``, optionally within one build.

    One seek on ``provider_operations_by_capability`` (or the build's primary
    key range); rows of other capabilities are never read.
    """

    sql = (
        "SELECT o.source_identity,p.artifact_digest,o.operation,o.effect "
        "FROM main.provider_operations o "
        "JOIN main.providers p ON p.identity=o.source_identity "
        "WHERE o.capability=? AND p.lifecycle='live'"
    )
    parameters: list[object] = [capability]
    if build is not None:
        sql += " AND o.source_identity=?"
        parameters.append(build)
    sql += " ORDER BY o.source_identity,o.operation"
    rows = tuple(
        ServingOperation(str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in connection.execute(sql, parameters)
    )
    BINDING_WORK["candidates"] += len(rows)
    return rows


# -- reading builds and the capability side -----------------------------------------------

#: ``(build identity, build digest) -> build``: a live build's accepted artifact.
BuildReader = Callable[[str, str], ProviderV2 | None]

#: Each build by digest: a build is immutable, so it is parsed once per process.
_BUILDS: ValueMemo[ProviderV2 | None] = ValueMemo(capacity=512)


def _memoized_build(digest: str, read: Callable[[], Any]) -> ProviderV2 | None:
    def load() -> ProviderV2 | None:
        BINDING_WORK["builds"] += 1
        found = read()
        return found if isinstance(found, ProviderV2) else None

    return _BUILDS.derive(digest, load)


def projection_build_reader(typed: Any) -> BuildReader:
    """Builds from a bound typed projection (one identity seek and member read per digest)."""

    return lambda identity, digest: _memoized_build(digest, lambda: typed.source(identity))


def tree_build_reader(tree: Mapping[str, bytes]) -> BuildReader:
    """Builds from an accepted or candidate tree."""

    from cruxible_client.contracts.providers import parse_provider, provider_path

    def read(identity: str, digest: str) -> ProviderV2 | None:
        def parse() -> Any:
            path = provider_path(identity.removeprefix("Provider:"))
            content = tree.get(path)
            return None if content is None else parse_provider(content, path=path)

        return _memoized_build(digest, parse)

    return read


@dataclass(frozen=True)
class CapabilitySide:
    """Where a slot's capability side resolves, with a cheap digest per CaptureKind.

    ``kind_digest`` keys the fit memo: it changes whenever the kind's declaration does.
    """

    side: BindingSide
    kind_digest: Callable[[str], str | None]


def tree_capability_side(tree: Mapping[str, bytes]) -> CapabilitySide:
    """The capability side over a (candidate or staged) tree."""

    from cruxible_client.contracts.canonical import file_digest
    from cruxible_client.contracts.captures import capture_contract_path, parse_capture_contract
    from cruxible_client.contracts.shapes import tree_contract_resolver

    kinds: dict[str, Any] = {}

    def capture_kinds(name: str) -> Any:
        if name not in kinds:
            path = capture_contract_path(name)
            content = tree.get(path)
            kinds[name] = (
                None
                if content is None
                else declared_kind(parse_capture_contract(content, path=path))
            )
        return kinds[name]

    def kind_digest(name: str) -> str | None:
        content = tree.get(capture_contract_path(name))
        return None if content is None else file_digest(content).tagged

    return CapabilitySide(BindingSide(tree_contract_resolver(tree), capture_kinds), kind_digest)


def projection_capability_side(typed: Any) -> CapabilitySide:
    """The capability side over a bound typed projection (identity seeks only)."""

    from cruxible_client.contracts.shapes import Contract, contract_digest, contract_identity

    contracts: dict[tuple[str, int], tuple[Contract, str] | None] = {}

    def contract(ref: Any) -> Contract | None:
        key = (ref.name, ref.version)
        if key not in contracts:
            value = typed.source(contract_identity(ref.name, ref.version).qualified)
            contracts[key] = (
                (value, contract_digest(value).tagged) if isinstance(value, Contract) else None
            )
        entry = contracts[key]
        if entry is None or (ref.digest is not None and entry[1] != ref.digest):
            return None
        return entry[0]

    kinds: dict[str, Any] = {}

    def capture_kinds(name: str) -> Any:
        if name not in kinds:
            kinds[name] = declared_kind(typed.source(f"CaptureContract:{name}"))
        return kinds[name]

    def kind_digest(name: str) -> str | None:
        row = typed.envelope(f"CaptureContract:{name}")
        return None if row is None else str(row.artifact_digest)

    return CapabilitySide(BindingSide(contract, capture_kinds), kind_digest)


def accepted_capability_side(instance: Any, coordinate: Any) -> CapabilitySide:
    """The capability side at one accepted coordinate, binding its projection per read."""

    def contract(ref: Any) -> Any:
        with instance.bind_accepted_projection(coordinate) as projection:
            return projection_capability_side(projection.typed).side.contracts(ref)

    kinds: dict[str, Any] = {}

    def capture_kinds(name: str) -> Any:
        if name not in kinds:
            with instance.bind_accepted_projection(coordinate) as projection:
                kinds[name] = projection_capability_side(projection.typed).side.capture_kinds(name)
        return kinds[name]

    def kind_digest(name: str) -> str | None:
        with instance.bind_accepted_projection(coordinate) as projection:
            return projection_capability_side(projection.typed).kind_digest(name)

    return CapabilitySide(BindingSide(contract, capture_kinds), kind_digest)


# -- the fit memo --------------------------------------------------------------------------


class _FitMemo:
    """Fits by (capability digest, truncation, build digest, operation), each valid while
    the instance kinds it consulted keep the digests it saw."""

    def __init__(self, capacity: int) -> None:
        self._entries: OrderedDict[
            tuple[object, ...], tuple[tuple[tuple[str, str | None], ...], BindingFit]
        ] = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()

    def get(
        self, key: tuple[object, ...], kind_digest: Callable[[str], str | None]
    ) -> BindingFit | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
        if entry is None:
            return None
        kinds, fit = entry
        if all(kind_digest(name) == digest for name, digest in kinds):
            return fit
        return None

    def put(
        self,
        key: tuple[object, ...],
        kinds: tuple[tuple[str, str | None], ...],
        fit: BindingFit,
    ) -> None:
        with self._lock:
            self._entries[key] = (kinds, fit)
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_FITS = _FitMemo(capacity=4096)


def memoized_slot_fitter(capability_side: CapabilitySide) -> SlotFitter:
    """A fitter over one capability side, memoized per process.

    The key is the slot's capability digest (which fixes its Contracts), its
    ``may_truncate``, the build digest (which fixes the operation and its carried
    copies) and the operation; an entry holds while every instance CaptureKind
    the fit consulted keeps its digest. A hit costs one digest read per such kind.
    """

    def fit(
        slot: str,
        need: SlotNeed,
        provider: ProviderV2,
        build_digest: str,
        operation: DeclaredOperation,
    ) -> BindingFit:
        del slot
        key = (
            need.pin.name,
            need.pin.version,
            need.pin.digest,
            need.may_truncate,
            build_digest,
            operation.name,
        )
        found = _FITS.get(key, capability_side.kind_digest)
        if found is not None:
            return found
        consulted: dict[str, None] = {}
        inner: CaptureKindLookup = capability_side.side.capture_kinds

        def recording(name: str) -> Any:
            consulted[name] = None
            return inner(name)

        BINDING_WORK["fits"] += 1
        result = slot_fit(
            need,
            operation,
            provider.declares,
            BindingSide(capability_side.side.contracts, recording),
        )
        _FITS.put(
            key, tuple((name, capability_side.kind_digest(name)) for name in consulted), result
        )
        return result

    return fit


# -- binding -------------------------------------------------------------------------------


def _repair(required_change: str) -> HandEditRepair:
    return HandEditRepair(
        hand_edit=HandEditInstruction(target="bindings", required_change=required_change)
    )


def _refuse(code: str, message: str, required_change: str) -> RequestRefusedError:
    return RequestRefusedError(code, message, repair=_repair(required_change))


#: ``(spelling, binding, build, operation)``: one operation offered for a slot.
_Candidate = tuple[str, LineBinding, ProviderV2 | None, DeclaredOperation | None]


def _build_spelling(binding: LineBinding) -> str:
    return f"{binding.build.target.name}#{binding.operation}"


def _choose(
    slot: str,
    need: SlotNeed,
    candidates: Sequence[_Candidate],
    *,
    fitter: SlotFitter,
    within: str | None = None,
    unfit: Sequence[tuple[str, BindingUnfitReason]] = (),
) -> LineBinding:
    """The one candidate that fits, or ``slot_unfilled`` / ``slot_ambiguous`` listing them.

    Each candidate is ``(spelling, binding, build, operation)``; a candidate with
    no build was already judged and is in ``unfit``.
    """

    fitting: list[tuple[str, LineBinding]] = []
    rejected = list(unfit)
    for spelling, binding, provider, operation in candidates:
        if provider is None or operation is None:
            continue
        if need.builtin:
            fitting.append((_build_spelling(binding), binding))
            continue
        fit = fitter(slot, need, provider, binding.build.artifact_digest, operation)
        if isinstance(fit, Fits):
            fitting.append((_build_spelling(binding), binding))
        else:
            rejected.append((spelling, fit))
    if len(fitting) == 1:
        return fitting[0][1]
    effect = need.capability.effect
    if not fitting:
        raise _refuse(
            "cruxible.line.slot_unfilled",
            slot_unfilled_message(
                slot,
                need.spelling,
                effect,
                unfit=rejected[:FIT_CANDIDATE_LIMIT],
                within=within,
            ),
            f"Install a build whose operation serves {need.spelling} and fits it, "
            f"or bind {slot} to one explicitly.",
        )
    raise _refuse(
        "cruxible.line.slot_ambiguous",
        slot_ambiguous_message(
            slot,
            need.spelling,
            effect,
            [spelling for spelling, _ in fitting[:FIT_CANDIDATE_LIMIT]],
            more=len(fitting) > FIT_CANDIDATE_LIMIT,
            within=within,
        ),
        f"Bind {slot} to one of them: build#operation.",
    )


def _spelling(build: str, operation: str) -> str:
    return operation_spelling(build, operation)


def _bind_unnamed(
    connection: sqlite3.Connection,
    slot: str,
    need: SlotNeed,
    *,
    fitter: SlotFitter,
    builds: BuildReader,
) -> LineBinding:
    """Fit a slot across every live build: seek the capability's name, keep equal effects
    and fitting operations; exactly one must remain."""

    candidates: list[_Candidate] = []
    unfit: list[tuple[str, BindingUnfitReason]] = []
    for row in serving_operations(connection, need.pin.name):
        if not need.builtin and row.effect != need.capability.effect:
            unfit.append(
                (
                    row.spelling,
                    EffectUnequal(
                        capability=need.capability.effect,
                        operation=row.effect,  # type: ignore[arg-type]
                    ),
                )
            )
            continue
        provider = builds(row.build, row.build_digest)
        operation = (
            None
            if provider is None
            else next((item for item in provider.operations if item.name == row.operation), None)
        )
        candidates.append((row.spelling, row.binding(), provider, operation))
    return _choose(slot, need, candidates, fitter=fitter, unfit=unfit)


def _bind_named(
    connection: sqlite3.Connection,
    slot: str,
    need: SlotNeed,
    reference: str,
    *,
    fitter: SlotFitter,
    builds: BuildReader,
) -> LineBinding:
    """A slot bound as the author names it: ``provider[/build][#operation]``.

    A named operation must pass ruling B1, serve the capability and fit. A named
    build without an operation binds its one operation that serves the
    capability and fits.
    """

    from cruxible_core.providers.builds import indexed_exact_provider, indexed_newest_provider

    name, operation_name = parse_binding_reference(reference)
    row = (
        indexed_exact_provider(connection, "Provider:" + name)
        if "/" in name
        else indexed_newest_provider(connection, name)
    )
    provider = None if row is None or not row.live else builds(row.identity, row.artifact_digest)
    if row is None or provider is None:
        raise _refuse(
            "cruxible.line.binding_unavailable",
            f"Slot {slot!r} names {reference!r}, which is not an installed live build.",
            "Bind a live installed build (provider list).",
        )
    build_name = row.identity.removeprefix("Provider:")
    pin = binding_pin(provider.identity, row.artifact_digest)
    if operation_name is not None:
        offered = [item for item in provider.operations if item.name == operation_name]
        if not offered:
            raise _refuse(
                "cruxible.line.binding_unavailable",
                f"Slot {slot!r} names {reference!r}; {build_name} declares no operation "
                f"{operation_name!r}.",
                f"Name an operation of {build_name} (provider list).",
            )
    else:
        serving = [item for item in provider.operations if need.pin.name in item.serves]
        offered = serving or (list(provider.operations) if len(provider.operations) == 1 else [])
        if not offered and need.builtin:
            offered = list(provider.operations[:1])
    if len(offered) == 1:
        (operation,) = offered
        spelling = _spelling(row.identity, operation.name)
        verdict = judge_slot_binding(
            slot, need, provider, row.artifact_digest, operation, spelling=spelling, fitter=fitter
        )
        if verdict.problem is not None:
            code, message = verdict.problem
            raise _refuse(
                code, message, f"Bind {slot} to an operation that serves {need.spelling}."
            )
        return LineBinding(build=pin, operation=operation.name)
    for operation in offered:
        problem = judge_slot_binding(
            slot,
            need,
            provider,
            row.artifact_digest,
            operation,
            spelling=_spelling(row.identity, operation.name),
            fitter=lambda *_args: Fits(fit="identical"),
        ).problem
        if problem is not None:
            code, message = problem
            raise _refuse(
                code, message, f"Bind {slot} to an operation that serves {need.spelling}."
            )
    return _choose(
        slot,
        need,
        [
            (
                _spelling(row.identity, item.name),
                LineBinding(build=pin, operation=item.name),
                provider,
                item,
            )
            for item in offered
        ],
        fitter=fitter,
        within=build_name,
    )


def parse_binding_reference(reference: str) -> tuple[str, str | None]:
    """``provider[/build][#operation]`` as ``(provider identity name, operation)``."""

    name, _, operation = reference.removeprefix("Provider:").partition("#")
    return name, operation or None


def resolve_slot_bindings(
    connection: sqlite3.Connection,
    slots: Mapping[str, SlotNeed],
    requested: Mapping[str, str],
    *,
    fitter: SlotFitter,
    builds: BuildReader,
) -> dict[str, LineBinding]:
    """Every slot's exact ``{build, operation}``: as named, or fitted (note §2.4).

    A requested ``provider[/build][#operation]`` names the build (a bare provider
    name is its most recently registered live build) and, optionally, the
    operation. A slot left out is fitted across every live build: exactly one
    operation must serve its capability with an equal effect and fit.
    ``fitter`` judges fit (its capability side resolves where the Line is
    authored); ``builds`` reads a build's operations and carried copies.
    """

    unknown = sorted(set(requested) - set(slots))
    if unknown:
        raise _refuse(
            "cruxible.procedure.slot_undeclared",
            f"Unknown slots: {unknown}; declared slots: {sorted(slots)}.",
            f"Bind only declared slots: {sorted(slots)}.",
        )
    resolved: dict[str, LineBinding] = {}
    for key, need in sorted(slots.items()):
        reference = requested.get(key)
        resolved[key] = (
            _bind_unnamed(connection, key, need, fitter=fitter, builds=builds)
            if reference is None
            else _bind_named(connection, key, need, reference, fitter=fitter, builds=builds)
        )
    return resolved


def line_slot_map(
    definition: Any,
    child: Callable[[ArtifactIdentity], Any],
    capabilities: CapabilityLookup,
) -> dict[str, SlotNeed]:
    """Every slot a Line over ``definition`` binds, invoked children's included.

    ``child`` reads the definition an invoke node names, and ``capabilities``
    resolves each slot's capability pin, at the authoring or admission
    coordinate.
    """

    from cruxible_client.contracts.procedures.line_specs import line_slot_needs
    from cruxible_core.capabilities.artifact import BUILTIN_CAPABILITY_NAMES

    return line_slot_needs(
        definition,
        capabilities=capabilities,
        builtin_names=BUILTIN_CAPABILITY_NAMES,
        child=line_child_definitions(definition, child),
    )


def line_capture_output_map(
    definition: Any,
    child: Callable[[ArtifactIdentity], Any],
) -> frozenset[str]:
    """Every capture output a Line over ``definition`` may name a rule for."""

    from cruxible_client.contracts.procedures.line_specs import line_capture_outputs

    return line_capture_outputs(definition, child=line_child_definitions(definition, child))


def line_child_definitions(
    definition: Any,
    child: Callable[[ArtifactIdentity], Any],
) -> dict[str, Any]:
    """Each invoked child's definition, keyed by its invoke node path (``a``, ``a.b``)."""

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
    return children


def child_bindings(bindings: Mapping[str, LineBinding], prefix: str) -> dict[str, LineBinding]:
    """The bindings of the slots one invoked child declares, keyed by its own slot names."""

    return {
        key[len(prefix) :]: value
        for key, value in bindings.items()
        if key.startswith(prefix) and "." not in key[len(prefix) :]
    }


__all__ = [
    "BINDING_WORK",
    "FIT_CANDIDATE_LIMIT",
    "BuildReader",
    "CapabilitySide",
    "accepted_capability_side",
    "ServingOperation",
    "memoized_slot_fitter",
    "operation_index_rows",
    "projection_build_reader",
    "projection_capability_side",
    "serving_operations",
    "tree_build_reader",
    "tree_capability_side",
    "child_bindings",
    "line_capture_output_map",
    "line_child_definitions",
    "line_slot_map",
    "parse_binding_reference",
    "resolve_slot_bindings",
]
