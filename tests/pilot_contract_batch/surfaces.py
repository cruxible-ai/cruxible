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

    import hashlib
    import json

    from cruxible_client.contracts.get_reads import GetProviderCard, GetRequest
    from cruxible_client.contracts.provider_registration import PackageRegistration
    from cruxible_client.contracts.providers import (
        ProviderLocalDistributionPin,
        ProviderLocalEnvBackendPin,
        ProviderRuntimeManifest,
        provider_path,
        render_provider,
    )
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.procedures.provider_installation import (
        _refuse_unproven_provider,
    )
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.core_support._identity_support import accept_tree

    # What the wheel carries, read the way ``package_metadata`` reads it: the
    # registration (core's schema-3 model) and the manifest it names by digest.
    registration = PackageRegistration.model_validate(package.registration)
    manifest_bytes = package.manifest_bytes
    if "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() != registration.manifest.digest:
        raise ValueError("the registration names another manifest")
    document = PackageRegistrationDocumentV1(
        schema_version=3,
        manifest=ProviderRuntimeManifest.model_validate(json.loads(manifest_bytes)),
        operations=registration.operations,
        declares=registration.declares,
        runtime_requirements=registration.runtime_requirements,
    )
    # The pins a wheel and lock would give; this pilot has neither, so stand-ins
    # derived from the registration bytes name one build per package.
    wheel = hashlib.sha256(json.dumps(package.registration, sort_keys=True).encode()).hexdigest()
    build = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=package.provider_id,
            version=package.version,
            filename=f"{package.provider_id.replace('-', '_')}-{package.version}-py3-none-any.whl",
            sha256="sha256:" + wheel,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + hashlib.sha256(b"lock:" + wheel.encode()).hexdigest(),
            materialization_digests={
                "pilot": "sha256:" + hashlib.sha256(b"env:" + wheel.encode()).hexdigest()
            },
        ),
        control_domain=package.provider_id,
        operations=document.pinned_operations(),
    )
    # The install's own law (no instance Contract is read), then the ordinary
    # proposal and acceptance of the one build path.
    _refuse_unproven_provider(build, frozenset())
    accept_tree(
        instance,
        {provider_path(build.identity.name): render_provider(build)},
        name=f"install-{package.provider_id}",
    )
    card = service_playbill_get(
        instance,
        request=GetRequest(ref=build.identity.qualified),
        access=BodyAccessContext(principal_id="pilot", can_read_body=False),
    ).card
    assert isinstance(card, GetProviderCard)
    return InstalledProvider(
        provider_id=card.provider,
        build=build.identity.qualified,
        operations=tuple(
            ListedOperation(
                name=row.operation,
                description=row.description,
                source=row.source.model_dump(mode="json"),
                serves=row.serves,
            )
            for row in card.operations
        ),
    )


# -- scenario 4: binding ----------------------------------------------------------------


def accept_line(instance: Any, line: Mapping[str, Any]) -> AcceptedLine:
    """Author and accept one Line (the ordinary ``line`` authoring input); its bindings.

    Binding by capability and fit happens here (the gate, note §2.4). A refusal
    raises ``RequestRefusedError`` whose code is one of ``cruxible.line.*`` (§2.6).
    Owner: binding (slice 4). Reused by scenarios 5 and 7.
    """

    import json

    from cruxible_client.contracts.authoring.inputs import LineInput
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, parse_line_spec
    from cruxible_core.authoring.lowering import AuthoringLoweringError
    from cruxible_core.errors import RequestRefusedError
    from tests.support.operation_world import author

    payload = LineInput.model_validate(dict(line))
    try:
        author(instance, payload, name=f"line-{line['name']}")
    except AuthoringLoweringError as exc:
        raise RequestRefusedError(exc.code, exc.message, repair=exc.repairs) from exc
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(str(line["name"]))
    accepted = parse_line_spec(tree[path], path=path)
    procedure = json.loads(tree["procedures/" + accepted.procedure.target.name + ".json"])
    pins = procedure["definition"]["slots"]
    fits = _binding_fits(instance, accepted)
    return AcceptedLine(
        name=accepted.identity.name,
        slots={
            slot: SlotBinding(
                slot=slot,
                operation=(f"{binding.build.target.name.partition('/')[0]}#{binding.operation}"),
                capability=CapabilityPin(
                    name=pins[slot]["capability"]["name"],
                    version=pins[slot]["capability"]["version"],
                    digest=pins[slot]["capability"]["digest"],
                ),
                fit=fits[slot],
            )
            for slot, binding in sorted(accepted.bindings.items())
        },
    )


def _binding_fits(instance: Any, line: Any) -> dict[str, Any]:
    """How each of an accepted Line's bindings fits its slot, judged at the head."""

    from cruxible_client.contracts.procedures.line_specs import (
        judge_slot_binding,
        operation_spelling,
    )
    from cruxible_core.capabilities.lookup import capability_in_projection
    from cruxible_core.providers.operation_index import (
        line_slot_map,
        memoized_slot_fitter,
        projection_build_reader,
        projection_capability_side,
    )

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        typed = projection.typed

        def child(identity: Any) -> Any:
            source = typed.source(identity.qualified)
            return None if source is None else source.definition

        procedure = typed.source(line.procedure.target.qualified)
        needs = line_slot_map(
            procedure.definition, child, lambda ref: capability_in_projection(projection, ref)
        )
        fitter = memoized_slot_fitter(projection_capability_side(typed))
        builds = projection_build_reader(typed)
        found: dict[str, Any] = {}
        for slot, binding in line.bindings.items():
            build = builds(binding.build.target.qualified, binding.build.artifact_digest)
            assert build is not None
            (operation,) = [item for item in build.operations if item.name == binding.operation]
            verdict = judge_slot_binding(
                slot,
                needs[slot],
                build,
                binding.build.artifact_digest,
                operation,
                spelling=operation_spelling(binding.build.target.name, binding.operation),
                fitter=fitter,
            )
            assert verdict.problem is None, verdict.problem
            found[slot] = verdict.fit
        return found


# -- scenario 5: swap -------------------------------------------------------------------


def run_slot_records(instance: Any, state: Any) -> dict[str, RunSlotRecord]:
    """Per slot, the capability pin and ``fit`` the run's receipts recorded (note §2.5).

    ``state`` is the ``ProcedureRunStateV2`` a Line run returned. Owner: binding
    with Capture-as-field (slices 4 and 6).
    """

    records: dict[str, RunSlotRecord] = {}
    for binding in state.receipt.resolved_provider_bindings:
        assert binding.capability.digest is not None and binding.fit is not None
        records[binding.slot] = RunSlotRecord(
            slot=binding.slot,
            capability=CapabilityPin(
                binding.capability.name, binding.capability.version, binding.capability.digest
            ),
            fit=binding.fit,
            operation_signature_digest=binding.operation_signature_digest,
        )
    return records


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

    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.captures import capture_contract_path, parse_capture_contract
    from tests.core_support._identity_support import accept_tree
    from tests.pilot_contract_batch.kit_capabilities import capability_slots
    from tests.support.layouts import grown_kind_members
    from tests.support.operation_world import author

    # The kind grows by the record sets and header fields it does not declare yet (a
    # layout only grows; the pilot's kit copy may already carry this one).
    path = capture_contract_path(kind)
    accepted = parse_capture_contract(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    held = accepted.layout
    records = {
        name: item
        for name, item in layout["records"].items()
        if held is None or name not in held.records
    }
    header = {
        name: rule
        for name, rule in (layout.get("header") or {}).items()
        if held is None or name not in held.header
    }
    if records or header:
        accept_tree(
            instance, grown_kind_members(instance, kind, records, header), name="kind-layout"
        )
    spec = dict(procedure)
    spec["slots"] = capability_slots(spec.get("slots", {}))
    payload = ProcedureInput.model_validate({"kind": "procedure", "spec": spec})
    # A successor only when the thin spec changes the accepted Procedure's definition
    # (the pilot's kit copy may already store this thin node).
    from cruxible_client.contracts.authoring.inputs import lower_authoring_input
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.proposals.proposals import AuthenticatedActor

    intent = (
        AuthoringIntentCoordinator.for_instance(instance)
        .create(
            actor=AuthenticatedActor(actor_id="owner"),
            payload=lower_authoring_input(payload),
            canonical_timestamp="2026-11-12T12:00:00.000000Z",
        )
        .intent
    )
    lowered = dict(lower_authoring(instance, intent=intent, actor_id="owner").changed_members)
    target = procedure_path(str(spec["procedure"]))
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    unchanged = (
        target in lowered
        and target in tree
        and parse_procedure(lowered[target], path=target).definition_digest
        == parse_procedure(tree[target], path=target).definition_digest
    )
    if not unchanged:
        author(instance, payload, name="thin")


# -- scenario 7: coverage ---------------------------------------------------------------


def layout_coverage_gap(instance: Any, *, kind: str, package: Any) -> str | None:
    """The first entry of the instance's layout of ``kind`` that ``package``'s carried
    layout copy does not cover (``None`` when it covers), per note §2.3.

    Owner: coverage (slice 7).
    """

    from cruxible_client.contracts.capability_fit import FitUnresolved
    from cruxible_client.contracts.capture_layouts import DeclaredCaptureKind, layout_covers
    from cruxible_client.contracts.provider_registration import PackageRegistration
    from cruxible_client.contracts.shapes import edge_refusal
    from cruxible_core.providers.operation_index import projection_capability_side

    declares = PackageRegistration.model_validate(package.registration).declares
    copy = declares.capture_kind(kind) or DeclaredCaptureKind(name=kind)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        side = projection_capability_side(projection.typed).side
        held = side.capture_kinds(kind) or DeclaredCaptureKind(name=kind)
        carried = declares.lookup()

        def type_fits(sender: Any, receiver: Any) -> Any:
            from cruxible_client.contracts.capability_fit import Fits, Unfit
            from cruxible_client.contracts.shapes import render_type

            refused = edge_refusal(sender, receiver)
            if refused is None:
                return Fits(fit="identical" if sender == receiver else "structural")
            return Unfit(
                side="result",
                path="value",
                problem="type",
                sender=render_type(sender),
                receiver=render_type(receiver),
            )

        def row_fits(sender: Any, receiver: Any) -> Any:
            from cruxible_client.contracts.capability_fit import record_fits

            try:
                return record_fits(
                    sender,
                    receiver,
                    carried,
                    side.contracts,
                    side="result",
                    capture_covers=lambda *_: None,
                )
            except FitUnresolved as exc:
                raise AssertionError(str(exc)) from exc

        # The provider produces the kind; the instance's declaration is what readers read.
        gap = layout_covers(copy, held, type_fits=type_fits, row_fits=row_fits)
    if gap is None:
        return None
    return (
        f"{gap.entry}: the provider's copy declares {gap.producer}; "
        f"the instance declares {gap.reader}"
    )


# -- scenario 8: kit requires kit -------------------------------------------------------


def kit_requires(bundle: Any) -> tuple[tuple[str, str], ...]:
    """A built kit bundle's ``requires`` as ``(kit_id, min_version)`` pairs.

    Owner: kit requires kit (slice 10).
    """

    return tuple((item.kit_id, item.min_version) for item in bundle.manifest.requires)
