"""Acceptance-layer pins: what world a procedure was accepted against.

A pin is a **payload plus its digest**, not a bare digest. A bare digest can be
compared and cannot be READ, so a mismatch says only "something changed" -- and
a receipt carrying one cannot reconstruct the accepted world at all.

Pins are an ACCEPTANCE-layer record, deliberately outside the definition
digest. The definition digest is computed at proposal, before compilation, by a
function with no instance and no lock; pins are resolved later, at acceptance.
Folding them into node content would make the digest uncomputable at both of
those sites. Keeping them apart also buys the useful query: same decision
point, different threshold -- which a design that conflated them cannot ask,
because re-pinning would mint a new subject.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from cruxible_core.errors import ConfigError
from cruxible_core.primitives import canonical_json
from cruxible_core.procedure.analysis import build_procedure_graph
from cruxible_core.procedure.resolution_oracle import compute_query_definition_digest
from cruxible_core.procedure.types import ProcedureRepeatStepSchema, unwrap_procedure_step

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_core.config.schema import CoreConfig
    from cruxible_core.procedure.types import ProcedureDefinition
    from cruxible_core.workflow.types import WorkflowLock

PinKind = Literal["provider", "query", "parameter", "artifact"]
PIN_KINDS: tuple[PinKind, ...] = ("provider", "query", "parameter", "artifact")

PROVIDER_PIN_FIELDS: tuple[str, ...] = (
    "version",
    "ref",
    "provider_entrypoint_digest",
    "provider_command_path",
    "runtime",
    "deterministic",
    "side_effects",
    "artifact",
    "config",
)
"""ALL NINE locked-provider fields.

`deterministic`, `side_effects` and `config` change what a provider is allowed
to do and does, so a pin that omitted them would not pin the accepted world --
it would pin a description of it.
"""

ARTIFACT_PIN_FIELDS: tuple[str, ...] = ("kind", "uri", "digest", "metadata")

QUERY_PIN_FIELDS: tuple[str, ...] = (
    "query_name",
    "query_definition_digest",
    "execution_options",
)
"""``execution_options`` is pinned SEPARATELY from the definition digest for the
reason the query-measurement contract already pins it separately: a runtime
override changes the question without changing the definition."""

PARAMETER_PIN_FIELDS: tuple[str, ...] = (
    "parameter_name",
    "revision_digest",
    "value_type",
    "value",
)
"""The VALUE is in the payload, so a run needs no parameter lookup at all."""

PIN_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "provider": PROVIDER_PIN_FIELDS,
    "query": QUERY_PIN_FIELDS,
    "parameter": PARAMETER_PIN_FIELDS,
    "artifact": ARTIFACT_PIN_FIELDS,
}


class AcceptanceNodePin(BaseModel):
    """One resolved dependency of one node, as accepted."""

    procedure_id: str
    node_id: str
    pin_kind: PinKind
    pin_key: str
    pin_payload: dict[str, Any]
    pin_digest: str

    model_config = ConfigDict(extra="forbid", frozen=True)


def compute_pin_digest(payload: dict[str, Any]) -> str:
    """Digest one pin payload."""
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def provider_pin_payload(locked: Any) -> dict[str, Any]:
    return {field: _jsonable(getattr(locked, field)) for field in PROVIDER_PIN_FIELDS}


def artifact_pin_payload(locked: Any) -> dict[str, Any]:
    return {field: _jsonable(getattr(locked, field)) for field in ARTIFACT_PIN_FIELDS}


def query_pin_payload(
    query_name: str,
    definition: Any,
    execution_options: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "query_name": query_name,
        "query_definition_digest": compute_query_definition_digest(definition),
        "execution_options": execution_options,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def build_acceptance_node_pins(
    *,
    procedure_id: str,
    definition: ProcedureDefinition,
    config: CoreConfig,
    lock: WorkflowLock,
) -> list[AcceptanceNodePin]:
    """Resolve the accepted world, one row per (node, dependency).

    One row finer than the coarse config/lock digests the record already
    carries, which is what lets a mismatch name the provider or query that
    moved instead of reporting that "the config changed".

    A repeat body's dependencies are attributed to the NAMESPACED nested node
    id (``"<repeat_id>/<nested_id>"``), the same id the node digests use.
    Attributing them to the container instead would leave two nested providers
    contributing rows that name a node whose Merkle identity does not exist, so
    no pin could be joined to the digest of the node it actually pins.
    """
    graph = build_procedure_graph(definition)
    pins: list[AcceptanceNodePin] = []
    for step in definition.steps:
        node_id = str(step.id)
        inner = unwrap_procedure_step(step)
        if isinstance(inner, ProcedureRepeatStepSchema):
            for nested in inner.repeat.steps:
                pins.extend(
                    _pins_for_step(procedure_id, f"{node_id}/{nested.id}", nested, config, lock)
                )
            continue
        pins.extend(_pins_for_step(procedure_id, node_id, inner, config, lock))
    # Deterministic order so the acceptance write and any later comparison
    # traverse the same sequence. Nested ids sort under their container.
    order = {node_id: index for index, node_id in enumerate(graph.node_ids)}
    pins.sort(
        key=lambda pin: (
            order.get(pin.node_id.split("/", 1)[0], len(order)),
            pin.node_id,
            pin.pin_kind,
            pin.pin_key,
        )
    )
    return pins


def expected_pin_keys(
    *,
    definition: ProcedureDefinition,
    config: CoreConfig,
    lock: WorkflowLock,
) -> set[tuple[str, str, str]]:
    """The ``(node_id, pin_kind, pin_key)`` set an acceptance of this definition writes.

    Completeness is a SET question, not a count and not a non-emptiness test. A
    definition whose nodes have no external dependencies at all -- guards,
    projections and input alone -- legitimately expects the EMPTY set, and a
    stored set missing one row of two is incomplete even though it is
    non-empty and its coarse digests still match.
    """
    return {
        (pin.node_id, str(pin.pin_kind), pin.pin_key)
        for pin in build_acceptance_node_pins(
            procedure_id="", definition=definition, config=config, lock=lock
        )
    }


def _pins_for_step(
    procedure_id: str,
    node_id: str,
    step: Any,
    config: CoreConfig,
    lock: WorkflowLock,
) -> list[AcceptanceNodePin]:
    pins: list[AcceptanceNodePin] = []
    provider_name = getattr(step, "provider", None)
    if provider_name is not None:
        locked = lock.providers.get(provider_name)
        if locked is None:
            raise ConfigError(
                f"Provider '{provider_name}' missing from lock file. Run `cruxible lock`."
            )
        pins.append(
            _pin(procedure_id, node_id, "provider", provider_name, provider_pin_payload(locked))
        )
        if locked.artifact is not None:
            locked_artifact = lock.artifacts.get(locked.artifact)
            if locked_artifact is None:
                raise ConfigError(
                    f"Artifact '{locked.artifact}' missing from lock file. Run `cruxible lock`."
                )
            pins.append(
                _pin(
                    procedure_id,
                    node_id,
                    "artifact",
                    locked.artifact,
                    artifact_pin_payload(locked_artifact),
                )
            )
    query = getattr(step, "query", None)
    if isinstance(query, str):
        schema = config.named_queries.get(query)
        if schema is None:
            raise ConfigError(f"Procedure references unknown query '{query}'")
        pins.append(
            _pin(
                procedure_id,
                node_id,
                "query",
                query,
                query_pin_payload(query, schema, _query_execution_options(step, schema)),
            )
        )
    return pins


def _query_execution_options(step: Any, schema: Any) -> dict[str, Any] | None:
    override = getattr(step, "relationship_state", None)
    options = {
        "relationship_state": override if isinstance(override, str) else schema.relationship_state,
        "result_shape": schema.result_shape,
        "dedupe": schema.dedupe,
    }
    return options


def _pin(
    procedure_id: str,
    node_id: str,
    pin_kind: PinKind,
    pin_key: str,
    payload: dict[str, Any],
) -> AcceptanceNodePin:
    return AcceptanceNodePin(
        procedure_id=procedure_id,
        node_id=node_id,
        pin_kind=pin_kind,
        pin_key=pin_key,
        pin_payload=payload,
        pin_digest=compute_pin_digest(payload),
    )


def verify_pin_integrity(pins: list[AcceptanceNodePin]) -> None:
    """(a) INTEGRITY -- every kind, always, with no external input.

    Recompute the digest from the stored payload. This proves only that the
    stored payload has not been altered since acceptance, which is exactly why
    it applies to every kind: it needs nothing from the current world.
    """
    for pin in pins:
        actual = compute_pin_digest(pin.pin_payload)
        if actual != pin.pin_digest:
            raise ConfigError(
                f"Acceptance pin for node '{pin.node_id}' ({pin.pin_kind} "
                f"'{pin.pin_key}') does not match its recorded digest "
                f"(stored={pin.pin_digest}, computed={actual}). The stored payload "
                "has been altered since acceptance; this is storage corruption or "
                "tampering, not drift."
            )


def verify_pin_currency(
    pins: list[AcceptanceNodePin],
    *,
    definition: ProcedureDefinition,
    config: CoreConfig,
    lock: WorkflowLock,
) -> None:
    """(b) CURRENCY -- only the kinds whose dependency is executable.

    Recomputed the same way acceptance built them, from the definition plus the
    config and lock in force, so an authored per-step override reads as
    authored content (already covered by the definition digest) rather than as
    drift.

    Parameter pins have NO currency check BY DESIGN, not omission. The pinned
    payload carries the value, so it IS the executable dependency; there is
    nothing external to compare against, and the only candidate -- the live
    revision -- is precisely what the pin exists to ignore. Comparing against it
    would turn every governed recalibration into a mass refusal of the
    procedures accepted under the old value.
    """
    current = {
        (pin.node_id, pin.pin_kind, pin.pin_key): pin.pin_payload
        for pin in build_acceptance_node_pins(
            procedure_id="", definition=definition, config=config, lock=lock
        )
    }
    for pin in pins:
        if pin.pin_kind == "parameter":
            continue
        key = (pin.node_id, pin.pin_kind, pin.pin_key)
        recomputed = current.get(key)
        if recomputed is None:
            raise ConfigError(
                f"Procedure node '{pin.node_id}' is pinned to {pin.pin_kind} "
                f"'{pin.pin_key}', which no longer resolves against the config and "
                "lock in force."
            )
        if recomputed == pin.pin_payload:
            continue
        differing = sorted(
            field
            for field in set(recomputed) | set(pin.pin_payload)
            if recomputed.get(field) != pin.pin_payload.get(field)
        )
        raise ConfigError(
            f"Procedure node '{pin.node_id}' is pinned to {pin.pin_kind} "
            f"'{pin.pin_key}' as accepted, which no longer matches this instance "
            f"(differing: {', '.join(differing)}). The run is refused rather than "
            "executed against an unreviewed model. Recover by re-proposing the "
            "definition for an independent reviewer to accept against the current "
            "config and lock, or by restoring the accepted config and re-running "
            "`cruxible lock`."
        )


def receipt_pin_material(
    pins: list[AcceptanceNodePin],
) -> tuple[dict[str, dict[str, dict[str, str]]], dict[str, dict[str, Any]]]:
    """Build the receipt's ``(node_pins, pin_payloads)`` pair.

    Payloads are deduplicated by digest across nodes, so a receipt is
    self-contained: a run id recovers the exact accepted world without
    consulting a config that may since have drifted. Aggregate config and lock
    digests cannot do that.
    """
    node_pins: dict[str, dict[str, dict[str, str]]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for pin in pins:
        node_pins.setdefault(pin.node_id, {}).setdefault(pin.pin_kind, {})[pin.pin_key] = (
            pin.pin_digest
        )
        payloads[pin.pin_digest] = pin.pin_payload
    return node_pins, payloads


__all__ = [
    "ARTIFACT_PIN_FIELDS",
    "PARAMETER_PIN_FIELDS",
    "PIN_KINDS",
    "PIN_PAYLOAD_FIELDS",
    "PROVIDER_PIN_FIELDS",
    "QUERY_PIN_FIELDS",
    "AcceptanceNodePin",
    "PinKind",
    "artifact_pin_payload",
    "build_acceptance_node_pins",
    "compute_pin_digest",
    "expected_pin_keys",
    "provider_pin_payload",
    "query_pin_payload",
    "receipt_pin_material",
    "verify_pin_currency",
    "verify_pin_integrity",
]
