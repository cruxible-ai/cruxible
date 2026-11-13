"""The calls the contract batch has not built yet: one per scenario, each with its owner.

Every helper here raises :class:`SurfaceMissing` until the slice that owns it
replaces the body with the real call. The owner implements the helper (and only
that helper's body), removes the ``xfail`` marker from its scenario, and makes
the scenario pass without editing its assertions. The return types below are
test-side records, not the batch's shared models: an owner fills them from
whatever the shared models and services return.

Several scenarios reuse an earlier scenario's helper as a precondition (the base
kit's capabilities, authoring a slot, installing a provider, accepting a Line).
Until that helper's owner lands, those scenarios stop at it; the transcript's
``STOPPED`` line names which helper stopped it.

Spellings follow the design note (2026-11-12): capability pins
``{name, version, digest}``; an authored slot ``{"capability": "name@N"}``;
registration schema 3 operations (``description``, ``source``, ``serves``,
``may_truncate``) with a ``declares`` block; layouts on the CaptureKind; a kit
manifest's ``requires: [{kit_id, min_version}]``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

BASE_KIT = "base kit (slice 9)"
SLOTS = "slots in authoring (slice 2)"
REGISTRATION = "registration and install (slice 3)"
BINDING = "binding (slice 4)"
SWAP = "binding (slice 4) with Capture-as-field (slice 6)"
LAYOUT = "layout and thin EXTRACT (slice 5)"
COVERAGE = "coverage (slice 7)"
KIT_REQUIRES = "kit requires kit (slice 10)"


class SurfaceMissing(NotImplementedError):
    """The call a scenario needs does not exist yet; ``owner`` builds it."""

    def __init__(self, helper: str, owner: str) -> None:
        self.helper, self.owner = helper, owner
        super().__init__(f"surfaces.{helper} is not implemented yet (owner: {owner})")


# -- records the helpers return ---------------------------------------------------------


@dataclass(frozen=True)
class CapabilityPin:
    name: str
    version: int
    digest: str

    @property
    def spelling(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True)
class CapabilityEntry:
    """One capability as the instance lists it."""

    pin: CapabilityPin
    effect: str
    #: The kit whose ``owns`` prefix holds it (``base`` for ``web.``/``workspace.``).
    kit_id: str | None


@dataclass(frozen=True)
class ListedOperation:
    """One operation of an installed provider build, as the provider listing shows it."""

    name: str
    description: str
    #: ``{"path": ..., "repository"?: ..., "revision"?: ...}`` as registered.
    source: Mapping[str, str]
    serves: tuple[str, ...]


@dataclass(frozen=True)
class InstalledProvider:
    provider_id: str
    #: ``Provider:<identity name>`` of the accepted build.
    build: str
    operations: tuple[ListedOperation, ...]


@dataclass(frozen=True)
class SlotBinding:
    """One slot of an accepted Line: what it binds and how it fits."""

    slot: str
    #: ``<provider identity name>#<operation>``, as refusals and listings spell it.
    operation: str
    capability: CapabilityPin
    fit: Literal["identical", "structural"]


@dataclass(frozen=True)
class AcceptedLine:
    name: str
    slots: Mapping[str, SlotBinding] = field(default_factory=dict)


@dataclass(frozen=True)
class RunSlotRecord:
    """What a run's receipts record for one call's slot."""

    slot: str
    capability: CapabilityPin
    fit: Literal["identical", "structural"]
    operation_signature_digest: str


# -- scenario 1: base kit ---------------------------------------------------------------


def list_capabilities(instance: Any) -> tuple[CapabilityEntry, ...]:
    """Every live capability the instance holds, sorted by name.

    Owner: base kit (slice 9), over the list surface slice 1 adds. Used as the
    precondition of scenarios 2, 4, 5, 7 and 8.
    """

    from cruxible_client.contracts.capabilities import capability_identity, parse_capability_ref
    from cruxible_core.service.discovery.orient import service_playbill_orient
    from cruxible_core.service.kits import _receipts

    listed = []
    cursor = None
    while True:
        page = service_playbill_orient(instance, section="capabilities", cursor=cursor)
        listed.extend(page.capabilities)
        cursor = page.next_cursor
        if cursor is None:
            break
    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    owners = [(receipt.kit_id, receipt.owns) for receipt in _receipts(instance, tree)]
    entries = []
    with instance.bind_accepted_projection(coordinate) as projection:
        for item in listed:
            ref = parse_capability_ref(item.capability)
            envelope = projection.typed.envelope(
                capability_identity(ref.name, ref.version).qualified
            )
            assert envelope is not None, item.capability
            kit_id = next(
                (kit for kit, owns in owners if any(ref.name.startswith(p) for p in owns)), None
            )
            entries.append(
                CapabilityEntry(
                    pin=CapabilityPin(ref.name, ref.version, envelope.artifact_digest),
                    effect=item.effect,
                    kit_id=kit_id,
                )
            )
    return tuple(sorted(entries, key=lambda entry: (entry.pin.name, entry.pin.version)))


# -- scenario 2: slots in authoring -----------------------------------------------------


def author_capability_procedure(instance: Any, spec: Mapping[str, Any]) -> dict[str, CapabilityPin]:
    """Author one Procedure from its authoring spec; each slot's recorded capability pin.

    ``spec`` is the ordinary Procedure authoring spec whose slots read
    ``{"capability": "name@N"}`` (optionally ``"may_truncate": true``). A refusal
    raises ``RequestRefusedError`` with its code. Owner: slots in authoring
    (slice 2). Reused by scenarios 4, 5 and 7.
    """

    import json

    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.procedures.artifacts import procedure_path
    from cruxible_core.authoring.lowering import AuthoringLoweringError
    from cruxible_core.errors import RequestRefusedError
    from tests.support.operation_world import author

    payload = ProcedureInput.model_validate({"kind": "procedure", **spec})
    try:
        author(instance, payload, name=str(spec["procedure"]))
    except AuthoringLoweringError as exc:
        raise RequestRefusedError(exc.code, exc.message, repair=exc.repairs) from exc
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    stored = json.loads(tree[procedure_path(str(spec["procedure"]))])["definition"]["slots"]
    return {
        slot: CapabilityPin(
            name=value["capability"]["name"],
            version=value["capability"]["version"],
            digest=value["capability"]["digest"],
        )
        for slot, value in sorted(stored.items())
    }


# -- scenario 3: registration and install -----------------------------------------------


def install_provider_package(instance: Any, package: Any) -> InstalledProvider:
    """Install one schema-3 test provider; the provider listing's view of its build.

    ``package`` is a :class:`tests.pilot_contract_batch.fixtures.providers.PilotPackage`
    (a schema-3 registration document with its ``declares`` block, a runtime
    manifest and plain Python entrypoints). The install must not need any kit or
    any Contract accepted on the instance. A refusal raises ``RequestRefusedError``.
    Owner: registration and install (slice 3). Reused by scenarios 4, 5 and 7.
    """

    raise SurfaceMissing("install_provider_package", REGISTRATION)


# -- scenario 4: binding ----------------------------------------------------------------


def accept_line(instance: Any, line: Mapping[str, Any]) -> AcceptedLine:
    """Author and accept one Line (the ordinary ``line`` authoring input); its bindings.

    Binding by capability and fit happens here (the gate, note §2.4). A refusal
    raises ``RequestRefusedError`` whose code is one of ``cruxible.line.*`` (§2.6).
    Owner: binding (slice 4). Reused by scenarios 5 and 7.
    """

    raise SurfaceMissing("accept_line", BINDING)


# -- scenario 5: swap -------------------------------------------------------------------


def run_slot_records(instance: Any, state: Any) -> dict[str, RunSlotRecord]:
    """Per slot, the capability pin and ``fit`` the run's receipts recorded (note §2.5).

    ``state`` is the ``ProcedureRunStateV2`` a Line run returned. Owner: binding
    with Capture-as-field (slices 4 and 6).
    """

    raise SurfaceMissing("run_slot_records", SWAP)


# -- scenario 6: layout and thin EXTRACT ------------------------------------------------


def adopt_layout_and_thin_extract(
    instance: Any, *, kind: str, layout: Mapping[str, Any], procedure: Mapping[str, Any]
) -> None:
    """Grow CaptureKind ``kind`` with ``layout`` and author ``procedure`` (a thin EXTRACT).

    ``layout`` is note §4.1's declaration; ``procedure`` is the kit's Procedure
    authoring spec with its extract node in the thin form of §4.3. Both land as
    ordinary authoring on the instance (a successor of the kind; a successor of
    the Procedure). Owner: layout and thin EXTRACT (slice 5).
    """

    raise SurfaceMissing("adopt_layout_and_thin_extract", LAYOUT)


# -- scenario 7: coverage ---------------------------------------------------------------


def layout_coverage_gap(instance: Any, *, kind: str, package: Any) -> str | None:
    """The first entry of the instance's layout of ``kind`` that ``package``'s carried
    layout copy does not cover (``None`` when it covers), per note §2.3.

    Owner: coverage (slice 7).
    """

    raise SurfaceMissing("layout_coverage_gap", COVERAGE)


# -- scenario 8: kit requires kit -------------------------------------------------------


def kit_requires(bundle: Any) -> tuple[tuple[str, str], ...]:
    """A built kit bundle's ``requires`` as ``(kit_id, min_version)`` pairs.

    Owner: kit requires kit (slice 10).
    """

    raise SurfaceMissing("kit_requires", KIT_REQUIRES)
