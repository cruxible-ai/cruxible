"""Kits: export owned definitions as a self-contained release; install one as a diff.

A release snapshots mutable definitions without their lineage. Immutable Contract
versions retain their exact accepted bytes, nested pins and predecessor chain.
Installing diffs the release against the consumer's accepted state. A missing
path is added as released; a changed mutable definition becomes a successor of
the consumer's current definition, and dropped mutable definitions may retire.
Mutable pins follow the digests the consumer holds. Contract commitments never
move: a conflicting version or a remap changing its pins refuses.
"""

from __future__ import annotations

import dataclasses
import functools
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Literal, Protocol

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_digest, pretty_canonical_bytes
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import parse_claim_type
from cruxible_client.contracts.documents import (
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    document_path,
    parse_document,
    render_document,
)
from cruxible_client.contracts.errors import (
    ProposalIntegrityError,
    ProposalStale,
    SettlementIntegrityError,
)
from cruxible_client.contracts.kits import (
    KIT_ARTIFACT_PREFIXES,
    KIT_RECEIPT_DOCUMENT_KIND,
    InstalledKit,
    KitAddRequest,
    KitArtifact,
    KitArtifactBytes,
    KitBuildRequest,
    KitBuildResult,
    KitBundle,
    KitChangeResult,
    KitConsequence,
    KitIdReusedRefusal,
    KitInstalledArtifact,
    KitKeptDivergence,
    KitManifest,
    KitOwnershipConflictRefusal,
    KitPathPlan,
    KitProvenance,
    KitProvider,
    KitProviderFileBytes,
    KitProviderStatus,
    KitProviderStep,
    KitReceipt,
    KitRemoveRequest,
    KitStatus,
    KitTransition,
    kit_artifact_path_allowed,
    kit_content_digest,
    kit_receipt_document_id,
    kit_version_key,
)
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionIdentityRule
from cruxible_client.contracts.projection import AcceptedCoordinate as ServedCoordinate
from cruxible_client.contracts.provider_installation import (
    AutomationCarryV1,
    ProviderInstallRequest,
    ProviderInstallResult,
    ProviderRepinV1,
    ProviderWheelObject,
    StoppedAutomationV1,
)
from cruxible_client.contracts.providers import (
    ProviderAny,
    ProviderV2,
    parse_provider,
    parse_provider_build_name,
    provider_digest,
    provider_path,
)
from cruxible_client.contracts.repairs import HandEditInstruction, HandEditRepair, RepairOperation
from cruxible_client.contracts.shapes import (
    Contract,
    contract_path_for_identity,
    evaluate_contract_law,
)
from cruxible_core.claims.artifact_references import (
    immutable_contract_error,
    movable_references,
    move_references,
    referenced_digests,
    referenced_identities,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeMigrationError,
    dependent_closure_inventory,
)
from cruxible_core.claims.closure import ArtifactDependencyStateV1, parse_dependency_artifact
from cruxible_core.derived.derived_state import fork_tree, paths_with_prefix
from cruxible_core.errors import ConfigError, DataValidationError, RequestRefusedError
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.ledger.bootstrap import genesis_seed_files
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.providers.automation_carry import carry_automation, parse_carry_line
from cruxible_core.providers.build_moves import (
    Mover,
    ProviderBuildMove,
    ProviderBuildMoveRefused,
    SettledMoves,
    build_move,
    live_triggers_of,
    settle_changes,
)
from cruxible_core.providers.builds import indexed_move_pinners, tree_provider_builds
from cruxible_core.providers.package_inspection import (
    InspectedProviderPackage,
    inspect_provider_package,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.change_preview import ChangeMode, admit_change_set, change_scope
from cruxible_core.service.procedures.provider_installation import ExpectedProviderBuild
from cruxible_core.service.proposals.proposals import open_proposal_at_target

# Definition families an owned artifact may pin. A pin into any other family is
# authority, binding or state, which a kit cannot carry, so the build refuses.
_INDEXED_PREFIXES: tuple[str, ...] = (
    *KIT_ARTIFACT_PREFIXES,
    "lines/",
    "procedure-mandates/",
    "providers/",
    "resolution-contracts/",
)
_RECEIPT_ACCESS = BodyAccessContext(principal_id="kit-receipt", can_read_body=True)
_PROVIDER_FILE_ACCESS = BodyAccessContext(principal_id="kit-provider", can_read_body=True)
_RECEIPT_SCOPE = ("kit",)
_SNAPSHOT_LIFECYCLE = {"predecessor_digest": None, "state": "live"}


@functools.cache
def _builtin_definitions() -> dict[tuple[str, str], frozenset[str]]:
    """(kind, name) -> the artifact digests of every compiler-seeded Provider and
    Contract in the genesis seeds (``cruxible-builtin`` and the ``workspace.file``
    operation's Contracts). They are on every instance, so a kit pins them as they
    are: it never carries, installs or remaps one."""

    found: dict[tuple[str, str], set[str]] = {}
    for path, content in genesis_seed_files().items():
        if path.startswith(("providers/", "contracts/")):
            state = _artifact_state(path, content)
            found.setdefault((state.identity.kind, state.identity.name), set()).add(
                state.artifact_digest
            )
    return {key: frozenset(digests) for key, digests in found.items()}


def _builtin(kind: str, name: str, digest: str) -> bool:
    return digest in _builtin_definitions().get((kind, name), frozenset())


def _builtin_refusals(
    tree: Mapping[str, bytes], contents: Mapping[str, bytes], *, by_name: bool = False
) -> list[str]:
    """A release pinning a built-in needs this instance to hold that exact one."""

    refused = []
    for path, content in sorted(contents.items()):
        state = parse_dependency_artifact(path, content, by_name=by_name)
        assert state is not None
        for pin in state.pins:
            if not isinstance(pin, ArtifactPin):
                continue
            if not _builtin(pin.target.kind, pin.target.name, pin.artifact_digest):
                continue
            held_path = (
                provider_path(pin.target.name)
                if pin.target.kind == "Provider"
                else contract_path_for_identity(pin.target.name)
            )
            held = tree.get(held_path)
            state = None if held is None else _artifact_state(held_path, held)
            if (
                state is None
                or state.artifact_digest != pin.artifact_digest
                or state.lifecycle.state != "live"
            ):
                refused.append(
                    f"{path} pins the built-in {pin.target.qualified} at "
                    f"{pin.artifact_digest[:19]}, which this instance does not hold live; "
                    "its genesis seeded another revision"
                )
    return sorted(set(refused))


def _without_lifecycle(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "lifecycle"}


def _references(path: str, payload: dict[str, Any]) -> Iterator[str]:
    return referenced_digests(path, payload)


def _substitute(path: str, payload: Mapping[str, Any], remap: Mapping[str, str]) -> dict[str, Any]:
    return move_references(path, payload, remap)


def _artifact_state(path: str, content: bytes) -> ArtifactDependencyStateV1:
    state = parse_dependency_artifact(path, content)
    if state is None:
        raise DataValidationError(f"{path} is not a definition artifact")
    if pretty_canonical_bytes(json.loads(content)) != content:
        raise DataValidationError(f"{path} is not in canonical form")
    return state


def _render(path: str, payload: dict[str, Any]) -> tuple[bytes, str]:
    if path.startswith("procedures/"):
        from cruxible_client.contracts.procedures.artifacts import (
            ProcedureArtifact,
            render_procedure,
        )

        content = render_procedure(ProcedureArtifact.model_validate(payload))
    elif (
        path.startswith("query-definitions/")
        and payload.get("artifact_format") == "cruxible-query-definition-v3"
    ):
        from cruxible_client.contracts.query.definitions import (
            QueryDefinition,
            render_query_definition,
        )

        content = render_query_definition(QueryDefinition.model_validate(payload))
    else:
        content = pretty_canonical_bytes(payload)
    return content, _artifact_state(path, content).artifact_digest


def _owned(state: ArtifactDependencyStateV1, owns: tuple[str, ...]) -> bool:
    return state.identity.name.startswith(owns)


def _dependency_order(
    states: Mapping[str, ArtifactDependencyStateV1],
    payloads: Mapping[str, dict[str, Any]],
    *,
    within: set[str] | None = None,
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Paths with the paths they pin, dependencies first; a cycle refuses."""

    by_digest = {state.artifact_digest: path for path, state in states.items()}
    by_identity = {
        (state.identity.kind, state.identity.name): path for path, state in states.items()
    }

    @functools.cache
    def contract_lookup(path: str) -> bytes | None:
        if path in payloads:
            return pretty_canonical_bytes(payloads[path])
        return genesis_seed_files().get(path)

    @functools.cache
    def exact(path: str) -> set[str]:
        if path.startswith("contracts/"):
            contract = Contract.model_validate(payloads[path])
            problems = evaluate_contract_law(
                contract,
                path=path,
                lookup=contract_lookup,
                parent_content=None,
                content=pretty_canonical_bytes(payloads[path]),
            )
            if problems:
                problem = problems[0]
                raise RequestRefusedError(
                    problem.code,
                    f"{path}: {problem.message}",
                    repair=HandEditRepair(
                        hand_edit=HandEditInstruction(
                            target=path,
                            required_change=(
                                "Supply the exact nested Contract versions and predecessor chain."
                            ),
                        )
                    ),
                )
        found = set()
        for pin in states[path].pins:
            target = by_digest.get(getattr(pin, "artifact_digest", "")) or by_identity.get(
                (pin.target.kind, pin.target.name)
            )
            if target is not None:
                found.add(target)
        for text in _references(path, payloads[path]):
            if text in by_digest:
                found.add(by_digest[text])
        found.discard(path)
        return found

    def named(path: str) -> set[str]:
        # Identity references decide what travels with a definition, never the
        # order: nothing re-pins them, so they cannot form a remapping cycle.
        found = {
            target
            for identity in referenced_identities(path, payloads[path])
            if (target := by_identity.get(identity)) is not None
        }
        found.discard(path)
        return found

    # Membership first, through both kinds of reference; then order that set by
    # exact dependencies alone, so an identity reference can never close a cycle.
    members: set[str] = set()
    pending = sorted(within if within is not None else states, reverse=True)
    while pending:
        path = pending.pop()
        if path in members:
            continue
        members.add(path)
        pending.extend(sorted(exact(path) | named(path), reverse=True))

    done: set[str] = set()
    visiting: set[str] = set()

    def visit(path: str) -> Iterator[tuple[str, tuple[str, ...]]]:
        if path in done:
            return
        if path in visiting:
            raise DataValidationError(f"definitions pin each other in a cycle through {path}")
        visiting.add(path)
        ordered = exact(path)
        for dependency in sorted(ordered):
            yield from visit(dependency)
        visiting.discard(path)
        done.add(path)
        yield path, tuple(sorted(ordered | named(path)))

    for path in sorted(members):
        yield from visit(path)


def _require_staged(instance: PlaybillInstance, files: Mapping[str, str], operation: str) -> None:
    """Refuse unless every named file (name -> sha256) is in the body store."""

    missing = sorted(
        name
        for name, digest in files.items()
        if instance.body_store().availability(digest) != "present"
    )
    if missing:
        raise RequestRefusedError(
            "cruxible.kit.provider_files_not_staged",
            f"{', '.join(missing)} not in this instance's body store; stage each file first "
            f"(the CLI and MCP {operation.rpartition('.')[2]} adapters stage what they send)",
            repair=RepairOperation(operation=operation),
        )


def _staged(instance: PlaybillInstance, digest: str, name: str) -> bytes:
    _require_staged(instance, {name: digest}, "cruxible.kit.build")
    return instance.body_store().read(digest, access=_PROVIDER_FILE_ACCESS)


def service_build_kit(
    instance: PlaybillInstance, request: KitBuildRequest, *, principal_id: str | None = None
) -> KitBuildResult:
    """Export owned definitions at the accepted head as one release of ``kit_id``.

    The manifest records where it was built (this instance, the accepted
    coordinate, the building principal): claimed, not proven. Each provider
    package named in ``request.providers`` is read from the body store, recorded
    in the manifest and carried as bytes in the returned bundle.
    """

    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    provenance = KitProvenance(
        instance_id=instance.descriptor.instance_id,
        coordinate=ServedCoordinate.model_validate(
            AcceptedCoordinate.from_internal(coordinate).model_dump(mode="json")
        ),
        principal_id=principal_id,
    )
    packages = []
    files: dict[str, bytes] = {}
    for item in request.providers:
        wheel = _staged(instance, item.wheel.digest, item.wheel.filename)
        lock = _staged(instance, item.lock_digest, "the provider lock")
        dependencies = tuple(
            (dependency.filename, _staged(instance, dependency.digest, dependency.filename))
            for dependency in item.dependencies
        )
        inspected = inspect_provider_package(
            wheel=(item.wheel.filename, wheel),
            lock=lock,
            dependencies=dependencies,
            delivery=item.delivery,
        )
        if item.delivery == "index":
            # A default provider installs by name: the kit carries none of its
            # files, and its path dependencies come from the index that serves it.
            packages.append(
                dataclasses.replace(
                    inspected,
                    provider=inspected.provider.model_copy(
                        update={"delivery": "index", "dependencies": ()}
                    ),
                )
            )
            continue
        packages.append(inspected)
        files[inspected.provider.wheel.filename] = wheel
        files[inspected.provider.lock.filename] = lock
        files.update(dependencies)
    bundle = build_kit(tree, request, provenance=provenance, providers=tuple(packages))
    return KitBuildResult(
        bundle=bundle.model_copy(
            update={
                "provider_files": tuple(
                    KitProviderFileBytes.of(name, files[name]) for name in sorted(files)
                )
            }
        )
        if files
        else bundle
    )


def _bundled_provider_pins(
    path: str,
    state: ArtifactDependencyStateV1,
    tree_providers: Mapping[str, ArtifactDependencyStateV1],
    tree: Mapping[str, bytes],
    bundled: Mapping[str, InspectedProviderPackage],
) -> None:
    """Every Provider a carried definition pins is bundled, as the build installed here.

    A kit never carries a Provider: on install it pins the Provider its bundled
    wheel registers, so that wheel must be the one this instance installed.
    """

    for pin in state.pins:
        if pin.target.kind != "Provider":
            continue
        held = tree_providers.get(provider_path(pin.target.name))
        digest = (
            pin.artifact_digest
            if isinstance(pin, ArtifactPin)
            else ""
            if held is None
            else held.artifact_digest
        )
        if _builtin("Provider", pin.target.name, digest):
            continue
        name = parse_provider_build_name(pin.target.name).name
        package = bundled.get(name)
        if package is None:
            raise RequestRefusedError(
                "cruxible.kit.provider_not_bundled",
                f"{path} pins Provider {name}, which this kit does not bundle; "
                "bundle its package (kit build --provider DIR)",
                repair=RepairOperation(operation="cruxible.kit.build"),
            )
        if package.provider.build != pin.target.name:
            raise RequestRefusedError(
                "cruxible.kit.provider_build_differs",
                f"{path} pins {pin.target.qualified}, another wheel and lock build "
                "than the bundled "
                f"{package.provider.package} {package.provider.version} "
                f"({package.provider.build}); move it to the bundled build "
                f"(provider move {name}) or bundle the build it pins",
                repair=RepairOperation(operation="cruxible.provider.move"),
            )
        provider_path_here = provider_path(pin.target.name)
        held = tree_providers.get(provider_path_here)
        provider = (
            None
            if held is None
            or (isinstance(pin, ArtifactPin) and held.artifact_digest != pin.artifact_digest)
            else parse_provider(tree[provider_path_here], path=provider_path_here)
        )
        differs = _build_difference(provider, package)
        if differs is not None:
            raise RequestRefusedError(
                "cruxible.kit.provider_build_differs",
                f"{path} pins Provider {pin.target.name} installed here from another build "
                f"than the bundled package ({differs}); bundle the wheel and lock this "
                "instance installed, or install the bundled ones here first",
                repair=RepairOperation(operation="cruxible.provider.install"),
            )


def _build_difference(
    provider: ProviderAny | None, package: InspectedProviderPackage
) -> str | None:
    """How the Provider held here differs from the bundled build, or None."""

    if not isinstance(provider, ProviderV2):
        return "no package Provider is held at that digest"
    runtime = provider.runtime_artifact
    if runtime.distribution.sha256 != package.provider.wheel.sha256:
        return f"wheel other than {package.provider.wheel.filename}"
    if runtime.local_env is None or runtime.local_env.lock_sha256 != package.provider.lock.sha256:
        return "lock other than the bundled one"
    if package.pin_key is None:
        # An index default given no path dependency wheels: its closure is the
        # index's, so only the wheel and lock it installs are compared.
        return None
    if runtime.local_env.materialization_digests.get(package.pin_key) != package.materialization:
        # The lock pins path dependencies by name and version only: the
        # materialization digest is what names their exact wheels.
        return "dependency closure other than the bundled wheels"
    return None


def build_kit(
    tree: Mapping[str, bytes],
    request: KitBuildRequest,
    *,
    provenance: KitProvenance | None = None,
    providers: tuple[InspectedProviderPackage, ...] = (),
) -> KitBundle:
    """Export the owned definitions of one accepted tree as a self-contained release.

    Mutable definitions become lineage-free snapshots; their pins move to the
    snapshot digests. Immutable Contract versions retain exact bytes and carry
    their nested closure and predecessor chain. A Provider pin names a bundled
    package instead.
    """

    bundled: dict[str, InspectedProviderPackage] = {}
    for package in providers:
        if bundled.setdefault(package.provider.provider_id, package) is not package:
            raise DataValidationError(
                f"two bundled packages register Provider {package.provider.provider_id}"
            )
    states: dict[str, ArtifactDependencyStateV1] = {}
    payloads: dict[str, dict[str, Any]] = {}
    tree_providers: dict[str, ArtifactDependencyStateV1] = {}
    for path in tree:
        if path.endswith(".json") and path.startswith(_INDEXED_PREFIXES):
            state = parse_dependency_artifact(path, tree[path])
            if state is None:
                continue
            if path.startswith("providers/"):
                # Never carried: a bundled package registers it on install.
                tree_providers[path] = state
                continue
            if _builtin(state.identity.kind, state.identity.name, state.artifact_digest):
                # A compiler-seeded Contract: pinned as it is, never carried.
                continue
            states[path] = state
            payloads[path] = json.loads(tree[path])
    roots = {
        path
        for path, state in states.items()
        if kit_artifact_path_allowed(path)
        and state.lifecycle.state == "live"
        and _owned(state, request.owns)
    }
    if not roots:
        raise DataValidationError(f"no live definitions are named under {', '.join(request.owns)}")
    remap: dict[str, str] = {}
    built: dict[str, bytes] = {}
    for path, pinned in _dependency_order(states, payloads, within=roots):
        for dependency in pinned:
            if not kit_artifact_path_allowed(dependency):
                raise DataValidationError(
                    f"{path} pins {dependency}, which a kit cannot carry; "
                    "a kit holds definitions, never authority, bindings or state"
                )
            if states[dependency].lifecycle.state != "live":
                raise DataValidationError(f"{path} pins retired {dependency}")
        _bundled_provider_pins(path, states[path], tree_providers, tree, bundled)
        payload = _substitute(path, payloads[path], remap)
        if not path.startswith("contracts/"):
            payload["lifecycle"] = dict(_SNAPSHOT_LIFECYCLE)
        content, digest = _render(path, payload)
        if digest != states[path].artifact_digest:
            remap[states[path].artifact_digest] = digest
        built[path] = content
    manifest = KitManifest(
        kit_id=request.kit_id,
        version=request.version,
        owns=request.owns,
        artifacts=tuple(
            KitArtifact(
                path=path, artifact_digest=_artifact_state(path, built[path]).artifact_digest
            )
            for path in sorted(built)
        ),
        providers=tuple(bundled[name].provider for name in sorted(bundled)),
        provenance=provenance,
    )
    return KitBundle(
        manifest=manifest,
        artifacts=tuple(KitArtifactBytes.of(path, built[path]) for path in sorted(built)),
    )


def _verify_bundle(bundle: KitBundle) -> dict[str, bytes]:
    contents = bundle.contents()
    digests = bundle.manifest.digests()
    for path, content in contents.items():
        state = _artifact_state(path, content)
        if state.artifact_digest != digests[path]:
            raise DataValidationError(f"{path} does not match its manifest digest")
        if state.lifecycle.state != "live" or state.lifecycle.predecessor_digest is not None:
            raise DataValidationError(
                f"{path} is not a snapshot; a release carries live definitions with no history"
            )
    return contents


def _receipt_at(
    instance: PlaybillInstance, path: str, raw: bytes, kit_id: str
) -> tuple[DocumentShell, KitReceipt] | None:
    """The receipt at ``path``, or None when an ordinary Document holds that name."""

    shell = parse_document(raw, path=path)
    if shell.document_kind != KIT_RECEIPT_DOCUMENT_KIND:
        return None
    body = instance.body_store().read(shell.body_digest, access=_RECEIPT_ACCESS)
    receipt = KitReceipt.model_validate_json(body)
    if receipt.kit_id != kit_id:
        raise ProposalIntegrityError(f"{path} records kit {receipt.kit_id}, not {kit_id}")
    return shell, receipt


def _read_receipt(
    instance: PlaybillInstance, tree: Mapping[str, bytes], kit_id: str
) -> tuple[DocumentShell, KitReceipt] | None:
    path = document_path(kit_receipt_document_id(kit_id))
    raw = tree.get(path)
    if raw is None:
        return None
    found = _receipt_at(instance, path, raw, kit_id)
    if found is None:
        raise DataValidationError(
            f"{path} is an ordinary Document, so kit {kit_id} has no receipt path"
        )
    return found


def _receipts(instance: PlaybillInstance, tree: Mapping[str, bytes]) -> Iterator[KitReceipt]:
    for path in paths_with_prefix(tree, "documents/kit-"):
        if path.endswith(".json"):
            kit_id = path.removeprefix("documents/kit-").removesuffix(".json")
            found = _receipt_at(instance, path, tree[path], kit_id)
            if found is not None:
                yield found[1]


def _successor_payload(
    payload: Mapping[str, Any], *, predecessor: str, state: str
) -> dict[str, Any]:
    if payload.get("artifact_format") == "cruxible-contract-v1":
        contract = Contract.model_validate(payload)
        raise immutable_contract_error(contract_path_for_identity(contract.identity.name))
    successor = dict(payload)
    successor["lifecycle"] = {"predecessor_digest": predecessor, "state": state}
    return successor


class _Diff:
    """One release diffed against one accepted tree."""

    def __init__(self) -> None:
        self.plan: list[KitPathPlan] = []
        self.writes: dict[str, bytes] = {}
        # Release digest -> the digest this instance holds (or will) for that path.
        self.installed: dict[str, str] = {}
        # Path -> the content digest (lifecycle apart) this instance holds after.
        self.content: dict[str, str] = {}
        # Divergences kept on purpose, recorded in the receipt.
        self.kept: list[KitKeptDivergence] = []
        # Identity -> dependents counted beyond the pin closure (evidence-rule
        # consumers of a CaptureContract, with their closures).
        self.semantic_counts: dict[str, int] = {}


def kit_back_remap(
    entries: Iterable[KitInstalledArtifact], current_digest: Callable[[str], str | None]
) -> dict[str, str]:
    """{this instance's digest of a kit member: the digest the receipt installed}.

    Only members whose held digest moved (a local succession) appear. Applied
    to a member's references, it undoes pins that merely followed another
    member's succession, so such a pin is not counted as an edit. Built once
    per receipt: O(kit members).
    """

    remap: dict[str, str] = {}
    for entry in entries:
        digest = current_digest(entry.path)
        if digest is not None and digest != entry.installed_digest:
            remap[digest] = entry.installed_digest
    return remap


def kit_content_matches(
    path: str, payload: Mapping[str, Any], recorded: str | None, back: Mapping[str, str]
) -> bool:
    """Whether a held kit member is its installed content, modulo pins that followed a member."""

    if recorded is None:
        return False
    if kit_content_digest(dict(payload)) == recorded:
        return True
    if not back or not movable_references(path):
        return False
    return kit_content_digest(move_references(path, payload, back)) == recorded


def _tree_back_remap(
    receipt_entries: Iterable[KitInstalledArtifact], tree: Mapping[str, bytes]
) -> dict[str, str]:
    def current(path: str) -> str | None:
        raw = tree.get(path)
        if raw is None:
            return None
        try:
            return _artifact_state(path, raw).artifact_digest
        except DataValidationError:
            return None

    return kit_back_remap(receipt_entries, current)


def _installed_content(entry: KitInstalledArtifact, tree: Mapping[str, bytes]) -> str | None:
    """The content digest a receipt recorded; receipts from before it use the bytes held."""

    if entry.content_digest is not None:
        return entry.content_digest
    current = tree.get(entry.path)
    if current is None:
        return None
    if _artifact_state(entry.path, current).artifact_digest != entry.installed_digest:
        return None
    return kit_content_digest(json.loads(current))


def _diff_release(
    tree: Mapping[str, bytes],
    contents: Mapping[str, bytes],
    *,
    owns: tuple[str, ...],
    installed: Mapping[str, KitInstalledArtifact],
    kept: Mapping[str, KitKeptDivergence],
    keep: frozenset[str],
    keep_local_edits: bool,
    preset: Mapping[str, str] | None = None,
) -> tuple[_Diff, set[str]]:
    """Plan every release path against the tree; returns the diff and the owned paths.

    A conflicting immutable Contract version refuses. A mutable definition held
    differently takes the release's version as a successor of its own,
    unless ``keep`` (or ``keep_local_edits``) keeps this instance's version or an
    earlier install kept it on purpose against this same release version.
    """

    states = {path: _artifact_state(path, content) for path, content in contents.items()}
    payloads = {path: json.loads(content) for path, content in contents.items()}
    owned = {path for path, state in states.items() if _owned(state, owns)}
    # A pin that followed a kit member's local succession is not an edit.
    back = _tree_back_remap(installed.values(), tree)
    diff = _Diff()
    # Digests the release pins outside itself, already moved to this instance's
    # (a bundled provider's Provider).
    diff.installed.update(preset or {})
    for path, _pinned in _dependency_order(states, payloads):
        release_digest = states[path].artifact_digest
        identity = states[path].identity.qualified
        payload = _substitute(path, payloads[path], diff.installed)
        carried = path not in owned
        current = tree.get(path)
        if current is None:
            content, digest = _render(path, payload)
            diff.plan.append(KitPathPlan(path=path, action="add", identity=identity))
            diff.writes[path] = content
            diff.installed[release_digest] = digest
            diff.content[path] = kit_content_digest(payload)
            continue
        current_state = _artifact_state(path, current)
        current_payload = json.loads(current)
        if path.startswith("contracts/") and current != contents[path]:
            raise immutable_contract_error(path)
        live = current_state.lifecycle.state == "live"
        if live and _without_lifecycle(current_payload) == _without_lifecycle(payload):
            diff.plan.append(KitPathPlan(path=path, action="unchanged", identity=identity))
            diff.installed[release_digest] = current_state.artifact_digest
            diff.content[path] = kit_content_digest(current_payload)
            continue
        previously = kept.get(path)
        if previously is not None and previously.release_digest == release_digest:
            # Kept on purpose against this same release version: not asked again.
            diff.plan.append(
                KitPathPlan(
                    path=path,
                    action="keep",
                    identity=identity,
                    consequence=previously.consequence,
                    detail="kept on purpose at an earlier install",
                )
            )
            diff.installed[release_digest] = current_state.artifact_digest
            diff.content[path] = kit_content_digest(current_payload)
            diff.kept.append(previously)
            continue
        consequence: KitConsequence | None
        if not live:
            consequence = "re_adds_retired"
        elif carried:
            consequence = "replaces_carried_definition"
        elif path not in installed:
            consequence = "takes_over_outside_definition"
        elif kit_content_matches(
            path, current_payload, _installed_content(installed[path], tree), back
        ):
            consequence = None
        else:
            consequence = "overwrites_your_edit"
        keeping = consequence is not None and (
            identity in keep or (keep_local_edits and consequence == "overwrites_your_edit")
        )
        if keeping:
            assert consequence is not None
            diff.plan.append(
                KitPathPlan(
                    path=path,
                    action="keep",
                    identity=identity,
                    consequence=consequence,
                )
            )
            diff.installed[release_digest] = current_state.artifact_digest
            diff.content[path] = kit_content_digest(current_payload)
            diff.kept.append(
                KitKeptDivergence(
                    path=path,
                    identity=identity,
                    consequence=consequence,
                    release_digest=release_digest,
                )
            )
            continue
        content, digest = _render(
            path,
            _successor_payload(payload, predecessor=current_state.artifact_digest, state="live"),
        )
        diff.plan.append(
            KitPathPlan(path=path, action="replace", identity=identity, consequence=consequence)
        )
        diff.writes[path] = content
        diff.installed[release_digest] = digest
        diff.content[path] = kit_content_digest(payload)
    return diff, owned


def _retirement(path: str, current: bytes, *, by_name: bool = False) -> bytes:
    state = _artifact_state(path, current)
    content, _digest = _render(
        path,
        _successor_payload(json.loads(current), predecessor=state.artifact_digest, state="retired"),
    )
    if not by_name:
        return content
    if path.startswith("query-definitions/"):
        from cruxible_client.contracts.query.definitions import (
            parse_query_definition,
            render_query_definition,
        )

        return render_query_definition(parse_query_definition(content, path=path))
    if path.startswith("triggers/"):
        from cruxible_client.contracts.triggers import parse_trigger, render_trigger

        return render_trigger(parse_trigger(content, path=path))
    if path.startswith("capture-contracts/"):
        from cruxible_client.contracts.capture_contract_inputs import (
            CaptureContractInputRecord,
            lower_capture_contract_input,
        )
        from cruxible_client.contracts.captures import (
            parse_capture_contract,
            render_capture_contract,
        )

        return render_capture_contract(
            lower_capture_contract_input(
                CaptureContractInputRecord(name=state.identity.name, lifecycle="retired"),
                predecessor=parse_capture_contract(current, path=path),
                named=True,
            )
        )
    return content


def _live_dependents(
    tree: Mapping[str, bytes], identity: ArtifactIdentity, path: str
) -> frozenset[str]:
    """The paths of the live reverse-pin closure of one definition."""

    try:
        return frozenset(
            item.path
            for item in dependent_closure_inventory(
                tree, roots=(identity,), fixed_paths=frozenset({path})
            )
        )
    except ClaimTypeMigrationError:
        return frozenset()


def _evidence_rule_consumers(
    tree: Mapping[str, bytes], contract: ArtifactDependencyStateV1
) -> tuple[tuple[str, ArtifactIdentity], ...]:
    """Live ClaimTypes whose evidence rules admit captures under this CaptureContract.

    A semantic dependency, not a pin: an identity rule (ClaimType v6 and later)
    names the contract by identity and a historical rule by exact digest, and
    neither is a reverse-pin edge. Retiring the contract strands every one of
    them, which admission refuses. Claims citing the contract are provenance and
    never count.
    """

    found = []
    for path in sorted(tree):
        if not (path.startswith("claim-types/") and path.endswith(".json")):
            continue
        try:
            claim_type = parse_claim_type(tree[path], path=path)
        except ValueError:
            continue
        if claim_type.lifecycle.state != "live":
            continue
        for rule in claim_type.evidence_admission_policy.rules:
            if isinstance(rule, ClaimEvidenceAdmissionIdentityRule):
                names = any(ref.target == contract.identity for ref in rule.capture_contracts)
            else:
                names = contract.artifact_digest in getattr(rule, "capture_contract_digests", ())
            if names:
                found.append((path, claim_type.identity))
                break
    return tuple(found)


def _settle_dropped(
    tree: Mapping[str, bytes],
    installed: Mapping[str, KitInstalledArtifact],
    keep_paths: set[str],
    diff: _Diff,
    *,
    keep: frozenset[str],
    retire_dependents: frozenset[str],
    kept: Mapping[str, KitKeptDivergence],
    by_name: bool = False,
) -> set[str]:
    """Decide each definition the kit installed that the release dropped.

    One decision per definition: kept live when named in ``keep``, retired with
    its dependents when named in ``retire_dependents``, otherwise retired when
    nothing live depends on it and kept (and reported) when something does.
    Returns the identities retired together with their dependents.
    """

    retiring_with_dependents: set[str] = set()
    # Dependents are counted on the tree this change leaves: a definition the
    # release replaces in the same change and that no longer pins the dropped
    # one is not its dependent.
    after = fork_tree(tree)
    for path, content in diff.writes.items():
        after[path] = content
    for path in sorted(installed):
        if path in keep_paths:
            continue
        current = tree.get(path)
        if current is None:
            continue
        state = _artifact_state(path, current)
        if state.lifecycle.state != "live":
            continue
        identity = state.identity.qualified
        previously = kept.get(path)
        dependent_paths = set(_live_dependents(after, state.identity, path))
        consumers = (
            _evidence_rule_consumers(after, state)
            if state.identity.kind == "CaptureContract"
            else ()
        )
        for consumer_path, consumer in consumers:
            dependent_paths.add(consumer_path)
            dependent_paths |= _live_dependents(after, consumer, consumer_path)
        dependent_paths.discard(path)
        dependents = len(dependent_paths)
        # Counted against the post-diff tree, including semantic consumers.
        diff.semantic_counts[identity] = dependents
        named_retire = identity in retire_dependents
        immutable = state.artifact_kind == "contract"
        if (
            immutable
            or identity in keep
            or (
                not named_retire
                and (dependents or (previously is not None and previously.release_digest is None))
            )
        ):
            diff.plan.append(
                KitPathPlan(
                    path=path,
                    action="keep",
                    identity=identity,
                    consequence="release_dropped",
                    dependent_count=dependents,
                    detail="Contract versions are immutable and remain accepted"
                    if immutable
                    else None
                    if identity in keep
                    else "the release dropped it and live artifacts depend on it; name it in "
                    "retire_dependents (--retire-dependents) to retire it and them"
                    if dependents
                    else "the release dropped it and an earlier install kept it on purpose; "
                    "name it in retire_dependents (--retire-dependents) to retire it",
                )
            )
            diff.kept.append(
                KitKeptDivergence(path=path, identity=identity, consequence="release_dropped")
            )
            continue
        diff.plan.append(
            KitPathPlan(
                path=path,
                action="retire",
                identity=identity,
                consequence="release_dropped",
                dependent_count=dependents,
            )
        )
        diff.writes[path] = _retirement(path, current, by_name=by_name)
        if dependents:
            retiring_with_dependents.add(identity)
        for consumer_path, consumer in consumers:
            # A ClaimType whose evidence rules need the retired contract retires
            # with it, and its own dependents with it.
            if consumer_path not in diff.writes:
                diff.writes[consumer_path] = _retirement(
                    consumer_path, tree[consumer_path], by_name=by_name
                )
            retiring_with_dependents.add(consumer.qualified)
    return retiring_with_dependents


def _settle_dependents(
    tree: Mapping[str, bytes],
    diff: _Diff,
    *,
    retire_with: set[str],
    connection: Any,
    movers: tuple[Mover, ...] = (),
    moves: Mapping[str, ProviderBuildMove] | None = None,
    keep_build: frozenset[str] = frozenset(),
    by_name: bool = False,
) -> SettledMoves:
    """Every write, plus one successor for each accepted dependent they change.

    A normal change set owes the closure of what it changes: every live artifact
    pinning a replaced or retired kit definition takes a successor in the same
    generation, carried (re-pinned) to the kit's final definitions, or retired
    with a definition the request retires together with its dependents. Each
    live Line bound to another build of a provider the kit ships moves to
    the kit's build (``movers``), unless ``keep_build`` names it. A Line,
    or Mandate over a replaced or moved Procedure is carried disabled or
    propose-only, respectively; Triggers stay untouched. The plan
    counts each definition's dependents rather than listing them.
    """

    settled = settle_changes(
        tree,
        writes=diff.writes,
        connection=connection,
        movers=movers,
        moves=moves,
        keep=keep_build,
        retire_with=frozenset(retire_with),
        by_name=by_name,
    )
    if not settled.refused:
        diff.plan[:] = [
            item.model_copy(update={"dependent_count": settled.counts[item.identity]})
            if item.identity in settled.counts and item.identity not in diff.semantic_counts
            else item
            for item in diff.plan
        ]
    return settled


def _kit_movers(
    connection: Any,
    tree: Mapping[str, bytes],
    providers: tuple[KitProvider, ...],
    writes: Mapping[str, bytes],
) -> tuple[Mover, ...]:
    """Each live Line bound to another build of a provider the kit ships.

    A kit carries no Lines, so every live binder of another build moves to the
    kit's build. O(affected live Line bindings), from the maintained name
    reverse-pin index.
    """

    found = []
    for provider in providers:
        for identity, path, source in indexed_move_pinners(
            connection,
            provider.provider_id,
            f"Provider:{provider.build}",
        ):
            if path in writes and _artifact_state(path, writes[path]).lifecycle.state != "live":
                continue
            kind, _, name = identity.partition(":")
            found.append(
                Mover(
                    identity=ArtifactIdentity(kind=kind, name=name),
                    path=path,
                    from_build=source.partition(":")[2],
                    to_build=provider.build,
                )
            )
    return tuple(sorted(found, key=lambda item: (item.identity.qualified, item.from_build)))


def _kit_moves(
    tree: Mapping[str, bytes], movers: tuple[Mover, ...]
) -> dict[str, ProviderBuildMove]:
    moves: dict[str, ProviderBuildMove] = {}
    for mover in movers:
        key = f"Provider:{mover.from_build}"
        if key not in moves:
            try:
                moves[key] = build_move(tree, mover.from_build, mover.to_build)
            except ProviderBuildMoveRefused:
                continue
    return moves


def _planned_automation(
    connection: Any,
    tree: Mapping[str, bytes],
    roots: tuple[ArtifactIdentity, ...],
    writes: Mapping[str, bytes],
    retire_with: set[str],
    *,
    by_name: bool = False,
) -> tuple[tuple[AutomationCarryV1, ...], tuple[StoppedAutomationV1, ...]]:
    """Affected automation before installation, with unavailable final digests.

    Closure errors block inventory. Retirement remains distinct from carry;
    discovery uses the same indexed closure and target lookups as settlement.
    """

    fixed = frozenset(writes)
    items = dependent_closure_inventory(tree, roots=roots, fixed_paths=fixed)
    retiring_procedures = {
        _artifact_state(path, content).identity.qualified
        for path, content in writes.items()
        if path.startswith("procedures/")
        and _artifact_state(path, content).lifecycle.state == "retired"
    }
    if retire_with:
        retired_items = dependent_closure_inventory(
            tree,
            roots=tuple(root for root in roots if root.qualified in retire_with),
            fixed_paths=fixed,
        )
        retiring_procedures.update(
            item.identity.qualified for item in retired_items if item.artifact_kind == "procedure"
        )
    carries = []
    stops: dict[str, StoppedAutomationV1] = {}
    for item in items:
        if item.artifact_kind not in {"line", "procedure-mandate"}:
            continue
        if (
            by_name
            and item.artifact_kind == "line"
            and item.triggering_identity.qualified not in retiring_procedures
        ):
            # A Line pins its Procedure's exact version, so a live Procedure
            # successor carries it disabled, as settlement does. Any other root
            # reaching a Line needs its own authoring and is not planned here.
            if item.triggering_identity.kind != "Procedure":
                continue
        elif by_name and item.artifact_kind == "line":
            line = parse_carry_line(tree[item.path], path=item.path)
            stops[line.identity.qualified] = StoppedAutomationV1(
                identity=line.identity.qualified,
                kind="line",
                stopped_by=line.procedure.target.qualified,
            )
            for trigger, _ in live_triggers_of(connection, line.identity.qualified):
                stops[trigger] = StoppedAutomationV1(
                    identity=trigger, kind="trigger", stopped_by=line.identity.qualified
                )
            continue
        row = carry_automation(item.path, tree[item.path], {}, planned=True)[1]
        stopped_by = item.triggering_identity.qualified
        if stopped_by not in retiring_procedures:
            carries.append(row)
            continue
        stops[row.identity] = StoppedAutomationV1(
            identity=row.identity, kind=row.kind, stopped_by=stopped_by
        )
        if row.kind == "line":
            for trigger, _ in live_triggers_of(connection, row.identity):
                stops[trigger] = StoppedAutomationV1(
                    identity=trigger, kind="trigger", stopped_by=row.identity
                )
    return (
        tuple(sorted(carries, key=lambda row: row.identity)),
        tuple(stops[key] for key in sorted(stops)),
    )


def _held_by_other_kits(receipts: tuple[KitReceipt, ...], kit_id: str) -> dict[str, str]:
    """Every definition path another installed kit holds -> that kit.

    A kit holds what its receipt records, not only what its current prefixes
    name: a definition the release dropped but the consumer kept stays that
    kit's even when a later release narrows its prefixes past it.
    """

    return {
        entry.path: receipt.kit_id
        for receipt in receipts
        if receipt.kit_id != kit_id
        for entry in receipt.artifacts
    }


def _ownership_refusal(
    manifest: KitManifest,
    owner: str,
    detail: str,
    *,
    owning_prefix: str | None = None,
    identity: str | None = None,
) -> KitOwnershipConflictRefusal:
    return KitOwnershipConflictRefusal(
        our_prefixes=manifest.owns,
        owning_kit_id=owner,
        owning_prefix=owning_prefix,
        identity=identity,
        detail=detail,
        repair=f"Remove the owning kit first: cruxible kit remove {owner}; or install "
        "a release that does not own the overlapping prefix or replace the held definition.",
    )


def _foreign_takeovers(
    receipts: tuple[KitReceipt, ...], manifest: KitManifest, diff: _Diff
) -> list[KitOwnershipConflictRefusal]:
    """A release may not change a definition another installed kit owns or holds: a
    hard block, like overlapping ownership."""

    owners = [
        (prefix, receipt.kit_id)
        for receipt in receipts
        if receipt.kit_id != manifest.kit_id and receipt.artifacts
        for prefix in receipt.owns
    ]
    held = _held_by_other_kits(receipts, manifest.kit_id)
    found: list[KitOwnershipConflictRefusal] = []
    for item in diff.plan:
        if item.action != "replace" or item.identity is None:
            continue
        name = item.identity.partition(":")[2]
        owned_by = [(prefix, owner) for prefix, owner in owners if name.startswith(prefix)]
        found.extend(
            _ownership_refusal(
                manifest,
                owner,
                f"{item.identity} is owned by kit {owner}",
                owning_prefix=prefix,
                identity=item.identity,
            )
            for prefix, owner in owned_by
        )
        holder = held.get(item.path)
        if holder is not None and not any(owner == holder for _, owner in owned_by):
            found.append(
                _ownership_refusal(
                    manifest,
                    holder,
                    f"{item.identity} is held by kit {holder}",
                    identity=item.identity,
                )
            )
    return found


def _ownership_conflicts(
    receipts: tuple[KitReceipt, ...], tree: Mapping[str, bytes], manifest: KitManifest
) -> list[KitOwnershipConflictRefusal]:
    conflicts = []
    for receipt in receipts:
        if receipt.kit_id == manifest.kit_id or not receipt.artifacts:
            continue
        for prefix in manifest.owns:
            for theirs in receipt.owns:
                if prefix.startswith(theirs) or theirs.startswith(prefix):
                    conflicts.append(
                        _ownership_refusal(
                            manifest,
                            receipt.kit_id,
                            f"{prefix} overlaps {theirs}, owned by kit {receipt.kit_id}",
                            owning_prefix=theirs,
                        )
                    )
    # A prefix may not take in a definition another kit still holds outside
    # its own prefixes (one it kept after its release dropped it).
    for path, holder in sorted(_held_by_other_kits(receipts, manifest.kit_id).items()):
        current = tree.get(path)
        if current is None:
            continue
        identity = _artifact_state(path, current).identity
        if any(identity.name.startswith(prefix) for prefix in manifest.owns) and not any(
            conflict.owning_kit_id == holder for conflict in conflicts
        ):
            conflicts.append(
                _ownership_refusal(
                    manifest,
                    holder,
                    f"{identity.qualified} is held by kit {holder}",
                    identity=identity.qualified,
                )
            )
    return conflicts


def _submit(
    instance: PlaybillInstance,
    mode: ChangeMode,
    *,
    kit_id: str,
    version: str | None,
    receipt: KitReceipt,
    previous_receipt: DocumentShell | None,
    writes: Mapping[str, bytes],
    plan: tuple[KitPathPlan, ...],
    actor_id: str,
    timestamp: str,
    transition: KitTransition | None = None,
    installed_version: str | None = None,
    provenance: KitProvenance | None = None,
    providers: tuple[KitProviderStep, ...] = (),
    repins: tuple[ProviderRepinV1, ...] = (),
    stops: tuple[StoppedAutomationV1, ...] = (),
    automation_carries: tuple[AutomationCarryV1, ...] = (),
) -> KitChangeResult:
    assert mode.head is not None
    base = mode.head
    described = {
        "transition": transition,
        "installed_version": installed_version,
        "provenance": provenance,
        "providers": providers,
        "repins": repins,
        "stops": stops,
        "automation_carries": automation_carries,
    }
    body = instance.store_document_body(pretty_canonical_bytes(receipt.model_dump(mode="json")))
    shell = DocumentShell(
        identity=f"document:{kit_receipt_document_id(kit_id)}",
        document_kind=KIT_RECEIPT_DOCUMENT_KIND,
        title=f"Kit {kit_id}" + ("" if version is None else f" {version}"),
        media_type="application/json",
        body_digest=body.digest,
        governance_scope=_RECEIPT_SCOPE,
        predecessor_digest=None
        if previous_receipt is None
        else document_digest(previous_receipt).tagged,
        lifecycle=DocumentLifecycle(
            revision=1 if previous_receipt is None else previous_receipt.lifecycle.revision + 1
        ),
    )
    candidate = instance.immutable_tree_at(base.git_oid).fork()
    for path, content in writes.items():
        candidate[path] = content
    candidate[document_path(kit_receipt_document_id(kit_id))] = render_document(shell)
    suffix = canonical_digest(
        "playbill-kit-change-target-v1",
        {
            "base": base.semantic_root,
            "kit": kit_id,
            "receipt": body.digest,
            "members": {path: content.hex() for path, content in sorted(writes.items())},
        },
    )
    target = f"refs/proposals/{actor_id}/kit-{kit_id}-{suffix[:32]}"
    pending = open_proposal_at_target(instance, target)
    if pending is not None:
        # The same change is already proposed: a commit returns it, and a
        # preview says the commit would.
        assert pending.candidate_digest is not None
        evidence = instance.proposal_evidence().read_candidate(pending.candidate_digest)
        return KitChangeResult(
            kit_id=kit_id,
            version=version,
            status="would_propose" if mode.previewing else "proposed",
            proposal_id=pending.proposal_id,
            approval_required=bool(evidence.approval_requirements),
            plan=plan,
            coordinate=mode.coordinate,
            **described,  # type: ignore[arg-type]
        )
    # Carries have no per-item reason: the accepting admission records why
    # this revision changed, for the shared get/floor reason resolver.
    if transition is None:
        rationale = f"Remove kit {kit_id} {version}."
    elif installed_version is None or transition == "reinstall":
        rationale = f"{transition.capitalize()} kit {kit_id} {version}."
    else:
        rationale = f"{transition.capitalize()} kit {kit_id} from {installed_version} to {version}."
    admitted = admit_change_set(
        instance,
        mode,
        actor_id=actor_id,
        request=ProposalAdmissionRequest(
            target_ref=target, proposed_base_oid=base.git_oid, rationale=rationale
        ),
        candidate_tree=candidate,
        timestamp=timestamp,
    )
    status: Literal[
        "unchanged", "proposed", "accepted", "blocked", "would_propose", "would_block"
    ] = admitted.status
    if status == "proposed" and not admitted.approval_required:
        # Like provider install and value writes, a kit change the approval
        # policy lets through lands at once; otherwise it stops at proposed.
        assert admitted.proposal_id is not None
        try:
            activation = service_activate_playbill_proposal(
                instance, proposal_id=admitted.proposal_id, activated_by=actor_id
            )
        except (SettlementIntegrityError, ProposalStale):
            # A concurrent add can publish after activation's stale check but
            # before settlement. Preserve integrity errors at the same head;
            # a genuinely moved preview gets the ordinary R12 refusal.
            with instance.accepted_head_held() as head_oid:
                if head_oid == base.git_oid:
                    raise
                mode.confirm_head(head_oid)
            return KitChangeResult(
                kit_id=kit_id,
                version=version,
                status="blocked",
                plan=plan,
                proposal_id=admitted.proposal_id,
                detail="Accepted state moved during kit add; preview the kit again.",
                coordinate=mode.coordinate,
                **described,  # type: ignore[arg-type]
            )
        if activation.status == "accepted":
            status = "accepted"
    return KitChangeResult(
        kit_id=kit_id,
        version=version,
        status=status,
        proposal_id=admitted.proposal_id,
        approval_required=admitted.approval_required,
        plan=plan,
        detail=None if admitted.admitted else admitted.refusal_detail(),
        coordinate=mode.coordinate,
        **described,  # type: ignore[arg-type]
    )


def _transition(installed: str | None, release: str) -> KitTransition:
    if installed is None:
        return "install"
    before, after = kit_version_key(installed), kit_version_key(release)
    return "upgrade" if after > before else "downgrade" if after < before else "reinstall"


class ProviderInstaller(Protocol):
    """Installs one provider package through the ordinary install (the API layer
    checks the install permission before it runs); ``expected_build`` pins an
    install by name to the published wheel and lock a kit's default names."""

    def __call__(
        self,
        request: ProviderInstallRequest,
        *,
        expected_build: ExpectedProviderBuild | None = None,
    ) -> ProviderInstallResult: ...


_ProviderState = Literal["installed", "differs", "missing", "retired"]


def _provider_here(
    tree: Mapping[str, bytes],
    provider: KitProvider,
    inspected: InspectedProviderPackage | None = None,
) -> tuple[_ProviderState, str | None, str | None]:
    """Whether this bundled build is live here: (state, digest, version).

    The build is its own artifact (``provider.build``), read at its exact path:
    another build of the provider never counts. ``differs`` is this build
    materialized here with another dependency closure; it is only known with
    ``inspected`` (the staged files, resolved for this host).
    """

    path = provider_path(provider.build)
    raw = tree.get(path)
    if raw is None:
        return "missing", None, None
    held = parse_provider(raw, path=path)
    digest = provider_digest(held).tagged
    if held.lifecycle.state != "live":
        return "retired", digest, None
    if not isinstance(held, ProviderV2):
        return "differs", digest, None
    same = inspected is None or _build_difference(held, inspected) is None
    return ("installed" if same else "differs"), digest, held.runtime_artifact.distribution.version


def _default_implementers(
    connection: Any,
    tree: Mapping[str, bytes],
    provider: KitProvider,
    contents: Mapping[str, bytes],
) -> tuple[str, ...]:
    """The live builds here that stand in for an index default, or none.

    A default provider stands for the operations the kit's Procedures' slots
    need, so it is skipped only when every slot the kit carries is already
    served by a live build of another provider here. The builds come from the
    signature index (one seek per slot), never a walk of every Provider.
    """

    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from cruxible_core.providers.operation_index import fitting_operations

    if provider.delivery != "index" or not provider.operations:
        return ()
    signatures = {
        slot.key(): slot
        for path, content in sorted(contents.items())
        if path.startswith("procedures/")
        for slot in parse_procedure(content, path=path).definition.slots.values()
    }
    if not signatures:
        return ()
    found: set[str] = set()
    for _key, signature in sorted(signatures.items()):
        serving = {
            item.build.removeprefix("Provider:")
            for item in fitting_operations(connection, signature)
            if parse_provider_build_name(item.build.removeprefix("Provider:")).name
            != provider.provider_id
        }
        if not serving:
            return ()
        found.update(serving)
    return tuple(sorted(found))


def _provider_steps(
    connection: Any,
    tree: Mapping[str, bytes],
    manifest: KitManifest,
    inspected: Mapping[str, InspectedProviderPackage] | None = None,
    *,
    contents: Mapping[str, bytes],
) -> tuple[list[KitProviderStep], list[KitProvider]]:
    """One step per provider package, and the ones still to install.

    The kit names one exact build per provider. Installing it adds that build
    beside any other build of the provider here, so another installed build
    never blocks: its Procedures move to the kit's build in the kit's change
    (``repins``). An index default another live Provider stands in for is not
    installed.
    """

    steps = []
    missing = []
    for provider in manifest.providers:
        state, _digest, _version = _provider_here(
            tree, provider, None if inspected is None else inspected.get(provider.provider_id)
        )
        named = {
            "provider_id": provider.provider_id,
            "package": provider.package,
            "version": provider.version,
            "build": provider.build,
        }
        implementers = (
            ()
            if state == "installed"
            else _default_implementers(connection, tree, provider, contents)
        )
        if state == "installed":
            steps.append(KitProviderStep(**named, action="unchanged"))
        elif implementers:
            steps.append(
                KitProviderStep(
                    **named,
                    action="satisfied",
                    detail=f"implemented here by {', '.join(implementers)}; the default "
                    "is not installed",
                )
            )
        elif state == "missing":
            missing.append(provider)
            steps.append(
                KitProviderStep(
                    **named,
                    action="would_install",
                    detail=(
                        f"by name from the provider index at exactly {provider.version}, "
                        "refusing any other build; refused where provider_repository is "
                        "configured"
                    )
                    if provider.delivery == "index"
                    else None,
                )
            )
        else:
            steps.append(
                KitProviderStep(
                    **named,
                    action="blocked",
                    detail=f"build {provider.build} is retired here; installing the kit "
                    "would restore it"
                    if state == "retired"
                    else f"build {provider.build} is installed here with another dependency "
                    "closure than the kit bundles (cruxible.provider.build_rematerialized); "
                    "the same wheel and lock name one build",
                )
            )
    return steps, missing


def _early_refusals(
    tree: Mapping[str, bytes],
    manifest: KitManifest,
    contents: Mapping[str, bytes],
    *,
    downgrade: bool,
    installed_version: str | None,
    by_name: bool = False,
) -> list[str]:
    """Refusals the current state decides, checked before any provider installs.

    A pinned built-in is either live here at exactly its digest or refused.
    """

    refused = _builtin_refusals(tree, contents, by_name=by_name)
    if downgrade:
        refused.append(
            f"release {manifest.version} is older than installed {installed_version}; "
            "pass allow_downgrade (--allow-downgrade) to install it"
        )
    return refused


def _inspect_staged(
    instance: PlaybillInstance, manifest: KitManifest
) -> dict[str, InspectedProviderPackage]:
    """Each bundled package read from the body store and resolved for this host.

    Metadata only, through the toolchain's readers: no provider code runs.
    """

    _require_staged(instance, manifest.provider_files(), "cruxible.kit.add")

    def read(name: str) -> bytes:
        return instance.body_store().read(
            manifest.provider_files()[name], access=_PROVIDER_FILE_ACCESS
        )

    return {
        provider.provider_id: inspect_provider_package(
            wheel=(provider.wheel.filename, read(provider.wheel.filename)),
            lock=read(provider.lock.filename),
            dependencies=tuple(
                (item.filename, read(item.filename)) for item in provider.dependencies
            ),
        )
        for provider in manifest.providers
        if provider.delivery == "bundled"
    }


def _install_providers(
    instance: PlaybillInstance,
    missing: list[KitProvider],
    steps: list[KitProviderStep],
    install: ProviderInstaller | None,
) -> list[KitProviderStep]:
    """Install each missing provider: a bundled one through the transfer install
    path, an index default by name at its exact version."""

    if install is None:
        raise ConfigError("this daemon cannot install the kit's providers")
    # The files were read from the body store already (_inspect_staged).
    by_id = {step.provider_id: index for index, step in enumerate(steps)}
    for provider in missing:
        if provider.delivery == "index":
            # Another build at the same name and version is refused before any of
            # it is fetched past its listing, prepared, registered or proposed.
            result = install(
                ProviderInstallRequest(
                    package=provider.package, version=provider.version, dry_run=False
                ),
                expected_build=ExpectedProviderBuild(
                    wheel_sha256=provider.wheel.sha256, lock_sha256=provider.lock.sha256
                ),
            )
        else:
            result = install(
                ProviderInstallRequest(
                    wheel=ProviderWheelObject(
                        filename=provider.wheel.filename, digest=provider.wheel.sha256
                    ),
                    lock_digest=provider.lock.sha256,
                    dependencies=tuple(
                        ProviderWheelObject(filename=item.filename, digest=item.sha256)
                        for item in provider.dependencies
                    ),
                    dry_run=False,
                )
            )
        index = by_id[provider.provider_id]
        if result.registered:
            update: dict[str, object] = {"action": "install", "detail": result.detail}
        elif result.status == "awaiting_approval":
            update = {"action": "awaiting_approval", "proposal_id": result.proposal_id}
        else:
            update = {
                "action": "blocked",
                "proposal_id": result.proposal_id,
                "detail": result.detail or f"install {result.status}",
            }
        steps[index] = steps[index].model_copy(update=update)
    return steps


def _provider_remap(
    tree: Mapping[str, bytes],
    contents: Mapping[str, bytes],
    manifest: KitManifest,
    inspected: Mapping[str, InspectedProviderPackage] | None = None,
) -> dict[str, str]:
    """Release Provider pin digest -> the digest of the same build installed here.

    A build's identity names its wheel and lock, so it is the same on every host;
    its digest includes the environment the host materialized (platform,
    machine, Python), so a release Procedure's pin keeps its identity and moves
    to the digest held here.
    """

    held = {}
    for provider in manifest.providers:
        state, digest, _version = _provider_here(
            tree, provider, None if inspected is None else inspected.get(provider.provider_id)
        )
        if state == "installed" and digest is not None:
            held[provider.build] = digest
    remap: dict[str, str] = {}
    for path, content in contents.items():
        for pin in _artifact_state(path, content).pins:
            if (
                isinstance(pin, ArtifactPin)
                and pin.target.kind == "Provider"
                and pin.target.name in held
            ):
                remap[pin.artifact_digest] = held[pin.target.name]
    return remap


def service_add_kit(
    instance: PlaybillInstance,
    request: KitAddRequest,
    *,
    actor_id: str,
    timestamp: str,
    install_provider: ProviderInstaller | None = None,
) -> KitChangeResult:
    """Propose the diff that brings this instance to one kit release.

    Derived across many artifacts, so it previews unless ``dry_run`` is false.
    A kit naming provider packages installs the missing ones first, through
    ``install_provider``; the definitions follow once they land. An index
    default another live Provider stands in for is not installed. A commit that
    stops at ``awaiting_providers`` is recorded, and the activation that lands
    the last awaited install proposes the definitions as this caller
    (``kit_resume``); any other commit of the kit replaces that record.
    """

    from cruxible_core.service import kit_resume

    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="cruxible.kit.add",
        describe=f"installing kit {request.bundle.manifest.kit_id}",
    ) as mode:
        result = _add_kit(
            instance,
            mode,
            request,
            actor_id=actor_id,
            timestamp=timestamp,
            install_provider=install_provider,
        )
        previewing = mode.previewing
    if previewing or kit_resume.resuming():
        return result
    if result.status == "awaiting_providers":
        awaited = {
            step.provider_id for step in result.providers if step.action == "awaiting_approval"
        }
        kit_resume.record_kit_resume(
            instance,
            request,
            actor_id=actor_id,
            waiting_on=(
                provider
                for provider in request.bundle.manifest.providers
                if provider.provider_id in awaited
            ),
        )
        return result.model_copy(
            update={
                "detail": (result.detail or "")
                + "; the definitions are proposed automatically, as you, once these "
                "installs activate (kit status shows the pending resume)"
            }
        )
    kit_resume.forget_kit_resume(instance, result.kit_id)
    return result


def _add_kit(
    instance: PlaybillInstance,
    mode: ChangeMode,
    request: KitAddRequest,
    *,
    actor_id: str,
    timestamp: str,
    install_provider: ProviderInstaller | None = None,
) -> KitChangeResult:
    assert mode.head is not None
    bundle = request.bundle
    manifest = bundle.manifest
    contents = _verify_bundle(bundle)
    tree = instance.immutable_tree_at(mode.head.git_oid)
    found = _read_receipt(instance, tree, manifest.kit_id)
    shell, receipt = (None, None) if found is None else found
    installed_version = None if receipt is None or not receipt.artifacts else receipt.version
    transition = _transition(installed_version, manifest.version)
    if (
        receipt is not None
        and receipt.artifacts
        and not any(
            ours.startswith(theirs) or theirs.startswith(ours)
            for ours in manifest.owns
            for theirs in receipt.owns
        )
    ):
        refusal = KitIdReusedRefusal(
            owning_kit_id=receipt.kit_id,
            our_prefixes=manifest.owns,
            installed_prefixes=receipt.owns,
            detail=f"kit id {manifest.kit_id} is installed with prefixes {receipt.owns}; "
            f"release prefixes {manifest.owns} share no prefix with that kit",
            repair=f"Remove the installed kit first: cruxible kit remove {receipt.kit_id}; "
            "then add this release.",
        )
        return KitChangeResult(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="would_block" if mode.previewing else "blocked",
            transition=transition,
            installed_version=installed_version,
            provenance=manifest.provenance,
            refusals=(refusal,),
            detail=refusal.detail,
            coordinate=mode.coordinate,
        )
    # Kit-add work only: indexed receipt documents and their recorded definitions,
    # never the instance's Claims or Subjects. Reuse this inventory for both guards.
    receipts = tuple(_receipts(instance, tree))
    ownership = _ownership_conflicts(receipts, tree, manifest)
    if ownership:
        return KitChangeResult(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="would_block" if mode.previewing else "blocked",
            transition=transition,
            installed_version=installed_version,
            provenance=manifest.provenance,
            refusals=tuple(ownership),
            detail="; ".join(item.detail for item in ownership),
            coordinate=mode.coordinate,
        )
    # A commit reads the staged provider files and resolves them for this host,
    # so a bundled build counts as installed only with its whole dependency
    # closure; a preview has no files and compares wheel and lock.
    inspected = (
        None if mode.previewing or not manifest.providers else _inspect_staged(instance, manifest)
    )
    for provider in manifest.providers:
        # The staged bytes must reproduce what the manifest says they are.
        if (
            inspected is not None
            and provider.delivery == "bundled"
            and inspected[provider.provider_id].provider != provider
        ):
            raise RequestRefusedError(
                "cruxible.kit.provider_manifest_mismatch",
                f"the staged files of bundled provider {provider.provider_id} do not "
                "reproduce its manifest entry (provider id, package, version, files or "
                "operations); rebuild the kit with kit build",
                repair=RepairOperation(operation="cruxible.kit.build"),
            )
    with instance.bind_accepted_projection(mode.head) as projection:
        steps, missing = _provider_steps(
            projection.typed.connection, tree, manifest, inspected, contents=contents
        )
    described: dict[str, object] = {
        "transition": transition,
        "installed_version": installed_version,
        "provenance": manifest.provenance,
        "providers": tuple(steps),
    }
    # The definitions pin what the bundled providers register, so they are
    # proposed only once those are live. A preview cannot install, so it plans
    # the definitions without evaluating them.
    awaiting_install = bool(missing) and not any(step.action == "blocked" for step in steps)
    if awaiting_install and not mode.previewing:
        # Every refusal the current state already decides comes before any
        # provider is prepared or installed.
        early = _early_refusals(
            tree,
            manifest,
            contents,
            downgrade=transition == "downgrade" and not request.allow_downgrade,
            installed_version=installed_version,
            by_name=True,
        )
        # A definition another installed kit owns or holds, which the release
        # would replace: the initial tree's diff decides it, with the same keep
        # decisions and the remap of the bundled providers already installed.
        initial, _owned_paths = _diff_release(
            tree,
            contents,
            owns=manifest.owns,
            installed={} if receipt is None else {item.path: item for item in receipt.artifacts},
            kept={} if receipt is None else {item.path: item for item in receipt.kept},
            keep=frozenset(request.keep),
            keep_local_edits=request.keep_local_edits,
            preset=_provider_remap(tree, contents, manifest, inspected),
        )
        ownership = _foreign_takeovers(receipts, manifest, initial)
        early.extend(item.detail for item in ownership)
        if early:
            return KitChangeResult(
                kit_id=manifest.kit_id,
                version=manifest.version,
                status="blocked",
                detail="; ".join(early),
                refusals=tuple(ownership),
                coordinate=mode.coordinate,
                **described,  # type: ignore[arg-type]
            )
        steps = _install_providers(instance, missing, steps, install_provider)
        described["providers"] = tuple(steps)
        pending = [step for step in steps if step.action in {"awaiting_approval", "blocked"}]
        if pending:
            return KitChangeResult(
                kit_id=manifest.kit_id,
                version=manifest.version,
                status="blocked"
                if any(step.action == "blocked" for step in pending)
                else "awaiting_providers",
                detail="the kit's definitions are proposed once its bundled providers are "
                "installed: "
                + "; ".join(
                    f"{step.provider_id} {step.action.replace('_', ' ')}"
                    + ("" if step.detail is None else f" ({step.detail})")
                    for step in pending
                ),
                coordinate=mode.coordinate,
                **described,  # type: ignore[arg-type]
            )
        # The installs moved the head; the definitions are pinned to the head
        # they produced.
        mode = ChangeMode(
            instance,
            previewing=False,
            at=None if mode.at is None else instance.accepted_coordinate().git_oid,
            kind="derived",
            operation="cruxible.kit.add",
            describe=f"installing kit {manifest.kit_id}",
        )
        assert mode.head is not None
        tree = instance.immutable_tree_at(mode.head.git_oid)
        awaiting_install = False
        assert inspected is not None
        satisfied = {step.provider_id for step in steps if step.action == "satisfied"}
        absent = [
            provider.provider_id
            for provider in manifest.providers
            if provider.provider_id not in satisfied
            and _provider_here(tree, provider, inspected.get(provider.provider_id))[0]
            != "installed"
        ]
        if absent:
            raise RequestRefusedError(
                "cruxible.kit.provider_not_installed",
                f"after installing, {', '.join(absent)} is not the live bundled build here; "
                "the definitions that pin it are not proposed",
                repair=RepairOperation(operation="cruxible.kit.status"),
            )
    assert mode.head is not None
    blocked = _builtin_refusals(tree, contents, by_name=True)
    blocked.extend(
        f"{step.provider_id}: {step.detail}" for step in steps if step.action == "blocked"
    )
    if transition == "downgrade" and not request.allow_downgrade:
        blocked.append(
            f"release {manifest.version} is older than installed {installed_version}; "
            "pass allow_downgrade (--allow-downgrade) to install it"
        )
    installed = {} if receipt is None else {item.path: item for item in receipt.artifacts}
    kept = {} if receipt is None else {item.path: item for item in receipt.kept}
    keep = frozenset(request.keep)
    diff, owned = _diff_release(
        tree,
        contents,
        owns=manifest.owns,
        installed=installed,
        kept=kept,
        keep=keep,
        keep_local_edits=request.keep_local_edits,
        preset=_provider_remap(tree, contents, manifest, inspected),
    )
    retire_with = _settle_dropped(
        tree,
        installed,
        owned,
        diff,
        keep=keep,
        retire_dependents=frozenset(request.retire_dependents),
        kept=kept,
        by_name=True,
    )
    ownership = _foreign_takeovers(receipts, manifest, diff)
    blocked.extend(item.detail for item in ownership)
    # Each live Line on another build of a provider the kit ships moves to
    # the kit's build in this same change; a satisfied index default installs no
    # build, so nothing moves for it.
    shipped = tuple(
        provider
        for provider, step in zip(manifest.providers, steps, strict=True)
        if step.action != "satisfied"
    )
    assert mode.head is not None
    with instance.bind_accepted_projection(mode.head) as projection:
        connection = projection.typed.connection
        movers = _kit_movers(connection, tree, shipped, diff.writes)
        if awaiting_install:
            # The kit's builds are not installed yet, so nothing can be moved or
            # evaluated: the preview names what the change will move and stop.
            repins = tuple(
                ProviderRepinV1(
                    identity=mover.identity.qualified,
                    from_build=mover.from_build,
                    to_build=mover.to_build,
                    action="kept",
                    reason="keep_build",
                )
                if mover.identity.qualified in request.keep_build
                else ProviderRepinV1(
                    identity=mover.identity.qualified,
                    from_build=mover.from_build,
                    to_build=mover.to_build,
                    action="successor",
                )
                for mover in movers
            )
            changed_roots = tuple(
                _artifact_state(path, tree[path]).identity for path in diff.writes if path in tree
            )
            stops: tuple[StoppedAutomationV1, ...]
            try:
                automation_carries, stops = _planned_automation(
                    connection,
                    tree,
                    (
                        *changed_roots,
                        *(
                            mover.identity
                            for mover in movers
                            if mover.identity.qualified not in request.keep_build
                        ),
                    ),
                    diff.writes,
                    retire_with,
                    by_name=True,
                )
            except (ClaimTypeMigrationError, ValueError) as exc:
                blocked.append(f"automation inventory incomplete: {exc}")
                automation_carries = ()
                stops = ()
            writes: dict[str, bytes] = dict(diff.writes)
        else:
            moves = _kit_moves(tree, movers)
            settled = _settle_dependents(
                tree,
                diff,
                retire_with=retire_with,
                connection=connection,
                movers=movers,
                moves=moves,
                keep_build=frozenset(request.keep_build),
                by_name=True,
            )
            blocked.extend(settled.refused)
            blocked.extend(
                f"{item.identity} cannot move to {item.to_build}: {item.reason}"
                for item in settled.repins
                if item.action == "refused"
            )
            writes = settled.writes if not settled.refused else dict(diff.writes)
            repins = tuple(settled.repins)
            stops = tuple(settled.stops)
            automation_carries = tuple(settled.automation_carries)
    described["repins"] = repins
    described["stops"] = stops
    described["automation_carries"] = automation_carries
    plan = tuple(sorted(diff.plan, key=lambda item: item.path))
    if blocked:
        return KitChangeResult(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="would_block" if mode.previewing else "blocked",
            plan=plan,
            detail="; ".join(blocked),
            refusals=tuple(ownership),
            coordinate=mode.coordinate,
            **described,  # type: ignore[arg-type]
        )

    for artifact in manifest.artifacts:
        if artifact.path in writes:
            state = _artifact_state(artifact.path, writes[artifact.path])
            diff.installed[artifact.artifact_digest] = state.artifact_digest
            diff.content[artifact.path] = kit_content_digest(json.loads(writes[artifact.path]))

    def entries(paths: set[str]) -> tuple[KitInstalledArtifact, ...]:
        return tuple(
            KitInstalledArtifact(
                path=item.path,
                release_digest=item.artifact_digest,
                installed_digest=diff.installed[item.artifact_digest],
                content_digest=diff.content[item.path],
            )
            for item in manifest.artifacts
            if item.path in paths
        )

    # A definition the release dropped but this instance kept stays the kit's:
    # its installation record carries forward, so status, removal, ownership
    # and the next install's decisions (keep it, or retire it and its
    # dependents) still see it.
    retained = tuple(
        installed[item.path]
        for item in sorted(diff.kept, key=lambda item: item.path)
        if item.consequence == "release_dropped" and item.path in installed
    )
    receipt_body = KitReceipt(
        kit_id=manifest.kit_id,
        version=manifest.version,
        content_digest=manifest.content_digest,
        owns=manifest.owns,
        artifacts=entries(owned) + retained,
        carried=entries(set(contents) - owned),
        source=request.source,
        kept=tuple(sorted(diff.kept, key=lambda item: item.path)),
        provenance=manifest.provenance,
        providers=manifest.providers,
    )
    if awaiting_install:
        return KitChangeResult(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="would_propose",
            plan=plan,
            detail="a commit installs the bundled providers first, then proposes the "
            "definitions, which are evaluated once those are installed",
            coordinate=mode.coordinate,
            **described,  # type: ignore[arg-type]
        )
    if not writes and receipt is not None and receipt == receipt_body:
        return KitChangeResult(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="unchanged",
            plan=plan,
            coordinate=mode.coordinate,
            **described,  # type: ignore[arg-type]
        )
    return _submit(
        instance,
        mode,
        kit_id=manifest.kit_id,
        version=manifest.version,
        receipt=receipt_body,
        previous_receipt=shell,
        writes=writes,
        plan=plan,
        actor_id=actor_id,
        timestamp=timestamp,
        **described,  # type: ignore[arg-type]
    )


def service_remove_kit(
    instance: PlaybillInstance,
    request: KitRemoveRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> KitChangeResult:
    """Propose retiring mutable definitions a kit owns; Contracts and carried ones stay.

    Derived across many artifacts, so it previews unless ``dry_run`` is false.
    """

    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="cruxible.kit.remove",
        describe=f"removing kit {request.kit_id}",
    ) as mode:
        return _remove_kit(instance, mode, request, actor_id=actor_id, timestamp=timestamp)


def _remove_kit(
    instance: PlaybillInstance,
    mode: ChangeMode,
    request: KitRemoveRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> KitChangeResult:
    assert mode.head is not None
    tree = instance.immutable_tree_at(mode.head.git_oid)
    found = _read_receipt(instance, tree, request.kit_id)
    if found is None or not found[1].artifacts:
        installed = sorted(
            receipt.kit_id for receipt in _receipts(instance, tree) if receipt.artifacts
        )
        named = ", ".join(installed) if installed else "none"
        raise RequestRefusedError(
            "cruxible.kit.not_installed",
            f"kit {request.kit_id!r} is not installed; installed: {named}",
            repair=RepairOperation(operation="cruxible.kit.status"),
        )
    shell, receipt = found
    diff = _Diff()
    edited = []
    back = _tree_back_remap((*receipt.artifacts, *receipt.carried), tree)
    for entry in receipt.artifacts:
        current = tree.get(entry.path)
        if current is None:
            continue
        state = _artifact_state(entry.path, current)
        if state.lifecycle.state != "live":
            continue
        if state.artifact_kind == "contract":
            diff.plan.append(
                KitPathPlan(
                    path=entry.path,
                    action="keep",
                    identity=state.identity.qualified,
                    detail="Contract versions are immutable and remain accepted",
                )
            )
            continue
        if not kit_content_matches(
            entry.path, json.loads(current), _installed_content(entry, tree), back
        ):
            edited.append(entry.path)
            continue
        diff.plan.append(
            KitPathPlan(path=entry.path, action="retire", identity=state.identity.qualified)
        )
        diff.writes[entry.path] = _retirement(entry.path, current, by_name=True)
    plan = tuple(sorted(diff.plan, key=lambda item: item.path))
    if edited:
        return KitChangeResult(
            kit_id=request.kit_id,
            version=receipt.version,
            status="would_block" if mode.previewing else "blocked",
            plan=plan,
            detail="edited kit paths must be reverted or retired by their own change first: "
            + ", ".join(edited),
            coordinate=mode.coordinate,
        )
    return _submit(
        instance,
        mode,
        kit_id=request.kit_id,
        version=receipt.version,
        receipt=receipt.model_copy(
            update={"artifacts": (), "carried": (), "kept": (), "providers": ()}
        ),
        previous_receipt=shell,
        writes=diff.writes,
        plan=plan,
        actor_id=actor_id,
        timestamp=timestamp,
    )


def service_kit_status(instance: PlaybillInstance) -> KitStatus:
    """Installed kits with the paths edited since install (by content, lineage apart)."""

    coordinate = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(coordinate.git_oid)
    kits = []
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        for receipt in _receipts(instance, tree):
            if not receipt.artifacts:
                continue
            kits.append(_installed_kit(connection, tree, receipt))
    from cruxible_core.service.kit_resume import kit_resume_statuses

    return KitStatus(kits=tuple(kits), resumes=kit_resume_statuses(instance))


def _installed_kit(connection: Any, tree: Mapping[str, bytes], receipt: KitReceipt) -> InstalledKit:
    """One installed kit's status: drifted paths and each provider build here."""

    back = _tree_back_remap((*receipt.artifacts, *receipt.carried), tree)
    drifted = tuple(
        entry.path
        for entry in receipt.artifacts
        if entry.path not in tree
        or not kit_content_matches(
            entry.path, json.loads(tree[entry.path]), _installed_content(entry, tree), back
        )
    )
    providers = []
    held = {
        entry.path: tree[entry.path]
        for entry in (*receipt.artifacts, *receipt.carried)
        if entry.path in tree
    }
    for provider in receipt.providers:
        state, _digest, version = _provider_here(tree, provider)
        others = [
            row
            for row in tree_provider_builds(tree, provider.provider_id)
            if row.live and row.identity != f"Provider:{provider.build}"
        ]
        if state != "installed" and _default_implementers(connection, tree, provider, held):
            shown: Literal["installed", "differs", "satisfied", "missing"] = "satisfied"
        elif state in {"missing", "retired"} and others:
            # The kit's build is not live, another build of it is.
            shown, version = "differs", max(row.version or "" for row in others) or None
        else:
            shown = "missing" if state == "retired" else state
        providers.append(
            KitProviderStatus(
                provider_id=provider.provider_id,
                package=provider.package,
                version=provider.version,
                wheel_sha256=provider.wheel.sha256,
                delivery=provider.delivery,
                state=shown,
                installed_version=version if shown == "differs" else None,
            )
        )
    return InstalledKit(
        kit_id=receipt.kit_id,
        version=receipt.version,
        content_digest=receipt.content_digest,
        source=receipt.source,
        drifted=drifted,
        provenance=receipt.provenance,
        kept=receipt.kept,
        providers=tuple(providers),
    )
