"""Kits: export owned definitions as a self-contained release; install one as a diff.

A release is a snapshot. Every artifact in it is lineage-free (no predecessor)
and pins only the release's own digests, so a release's content digest names the
same definitions wherever it goes. History belongs to the consumer: installing
diffs the release against the consumer's accepted state and proposes that diff.
A path the consumer lacks is added as released; a path whose content changed is
replaced by a successor naming the consumer's own current digest; a path the kit
installed and the release no longer has is retired. Pins are remapped from
release digests to the digests the consumer actually holds, so a dependent whose
definition only moved in lineage compares equal and stays untouched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from cruxible_client.contracts.artifacts import ArtifactIdentity
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
from cruxible_client.contracts.errors import ProposalIntegrityError
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
    KitInstalledArtifact,
    KitKeptDivergence,
    KitManifest,
    KitPathPlan,
    KitProvenance,
    KitReceipt,
    KitRemoveRequest,
    KitStatus,
    KitTransition,
    kit_artifact_path_allowed,
    kit_content_digest,
    kit_receipt_document_id,
    kit_version_key,
)
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionRule
from cruxible_client.contracts.projection import AcceptedCoordinate as ServedCoordinate
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.claims.artifact_references import (
    move_references,
    referenced_digests,
    referenced_identities,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    ClaimTypeMigrationInventoryItemV1,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.claims.closure import ArtifactDependencyStateV1, parse_dependency_artifact
from cruxible_core.errors import DataValidationError, RequestRefusedError
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.change_preview import ChangeMode, admit_change_set, change_scope
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals

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
_RECEIPT_SCOPE = ("kit",)
_SNAPSHOT_LIFECYCLE = {"predecessor_digest": None, "state": "live"}


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
    content = pretty_canonical_bytes(payload)
    return content, _artifact_state(path, content).artifact_digest


def _owned(state: ArtifactDependencyStateV1, owns: tuple[str, ...]) -> bool:
    # A ProviderInterface belongs to the provider package that registers it: a
    # kit carries it for its Blueprints and Procedures and never owns it, so it
    # never replaces or retires one.
    return state.artifact_kind != "provider-interface" and state.identity.name.startswith(owns)


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

    def exact(path: str) -> set[str]:
        found = set()
        for pin in states[path].pins:
            target = by_digest.get(pin.artifact_digest) or by_identity.get(
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


def service_build_kit(
    instance: PlaybillInstance, request: KitBuildRequest, *, principal_id: str | None = None
) -> KitBuildResult:
    """Export owned definitions at the accepted head as one release of ``kit_id``.

    The manifest records where it was built (this instance, the accepted
    coordinate, the building principal): claimed, not proven.
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
    return KitBuildResult(bundle=build_kit(tree, request, provenance=provenance))


def build_kit(
    tree: Mapping[str, bytes],
    request: KitBuildRequest,
    *,
    provenance: KitProvenance | None = None,
) -> KitBundle:
    """Export the owned definitions of one accepted tree as a self-contained release.

    Owned live definitions and everything they pin become lineage-free
    snapshots; each pin moves to the snapshot digest of what it names.
    """

    states: dict[str, ArtifactDependencyStateV1] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for path in tree:
        if path.endswith(".json") and path.startswith(_INDEXED_PREFIXES):
            state = parse_dependency_artifact(path, tree[path])
            if state is not None:
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
        payload = _substitute(path, payloads[path], remap)
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
    for path in sorted(tree):
        if path.startswith("documents/kit-") and path.endswith(".json"):
            kit_id = path.removeprefix("documents/kit-").removesuffix(".json")
            found = _receipt_at(instance, path, tree[path], kit_id)
            if found is not None:
                yield found[1]


def _successor_payload(
    payload: Mapping[str, Any], *, predecessor: str, state: str
) -> dict[str, Any]:
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
) -> tuple[_Diff, set[str]]:
    """Plan every release path against the tree; returns the diff and the owned paths.

    Nothing blocks: a definition this instance holds differently takes the
    release's version as a successor of its own, named by what that does here,
    unless ``keep`` (or ``keep_local_edits``) keeps this instance's version or an
    earlier install kept it on purpose against this same release version.
    """

    states = {path: _artifact_state(path, content) for path, content in contents.items()}
    payloads = {path: json.loads(content) for path, content in contents.items()}
    owned = {path for path, state in states.items() if _owned(state, owns)}
    diff = _Diff()
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
        elif _installed_content(installed[path], tree) == kit_content_digest(current_payload):
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
                    detail="this instance's version is kept",
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


def _retirement(path: str, current: bytes) -> bytes:
    state = _artifact_state(path, current)
    content, _digest = _render(
        path,
        _successor_payload(json.loads(current), predecessor=state.artifact_digest, state="retired"),
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
            if isinstance(rule, ClaimEvidenceAdmissionRule):
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
) -> set[str]:
    """Decide each definition the kit installed that the release dropped.

    One decision per definition: kept live when named in ``keep``, retired with
    its dependents when named in ``retire_dependents``, otherwise retired when
    nothing live depends on it and kept (and reported) when something does.
    Returns the identities retired together with their dependents.
    """

    retiring_with_dependents: set[str] = set()
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
        dependent_paths = set(_live_dependents(tree, state.identity, path))
        consumers = (
            _evidence_rule_consumers(tree, state)
            if state.identity.kind == "CaptureContract"
            else ()
        )
        for consumer_path, consumer in consumers:
            dependent_paths.add(consumer_path)
            dependent_paths |= _live_dependents(tree, consumer, consumer_path)
        dependent_paths.discard(path)
        dependents = len(dependent_paths)
        if consumers:
            # Counted here in full; the pin-closure recount must not shrink it.
            diff.semantic_counts[identity] = dependents
        named_retire = identity in retire_dependents
        if identity in keep or (
            not named_retire
            and (dependents or (previously is not None and previously.release_digest is None))
        ):
            diff.plan.append(
                KitPathPlan(
                    path=path,
                    action="keep",
                    identity=identity,
                    consequence="release_dropped",
                    dependent_count=dependents,
                    detail=None
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
        diff.writes[path] = _retirement(path, current)
        if dependents:
            retiring_with_dependents.add(identity)
        for consumer_path, consumer in consumers:
            # A ClaimType whose evidence rules need the retired contract retires
            # with it, and its own dependents with it.
            if consumer_path not in diff.writes:
                diff.writes[consumer_path] = _retirement(consumer_path, tree[consumer_path])
            retiring_with_dependents.add(consumer.qualified)
    return retiring_with_dependents


def _settle_dependents(
    tree: Mapping[str, bytes],
    diff: _Diff,
    *,
    retire_with: set[str],
) -> tuple[dict[str, bytes], list[str]]:
    """Every write, plus one successor for each accepted dependent they change.

    A normal change set owes the closure of what it changes: every live artifact
    pinning a replaced or retired kit definition takes a successor in the same
    generation, carried (re-pinned) to the kit's final definitions, or retired
    with a definition the request retires together with its dependents. The plan
    counts each definition's dependents rather than listing them.
    """

    changed = {path: content for path, content in diff.writes.items() if path in tree}
    if not changed:
        return dict(diff.writes), []
    roots: dict[str, ArtifactIdentity] = {}
    for path in sorted(changed):
        identity = _artifact_state(path, tree[path]).identity
        roots[identity.qualified] = identity
    fixed = frozenset(diff.writes)
    try:
        # Each changed definition's complete reverse closure, on its own: a
        # dependent reached through several definitions counts for each, and
        # belongs to the retirement closure of every one it reaches.
        closures = {
            name: dependent_closure_inventory(tree, roots=(identity,), fixed_paths=fixed)
            for name, identity in roots.items()
        }
    except ClaimTypeMigrationError as error:
        return dict(diff.writes), [str(error)]
    union: dict[str, ClaimTypeMigrationInventoryItemV1] = {}
    for name in roots:
        for item in closures[name]:
            union.setdefault(item.path, item)
    if not union:
        return dict(diff.writes), []
    inventory = tuple(union[path] for path in sorted(union))
    counts = {name: len(items) for name, items in closures.items() if items}
    retiring = {item.path for name in retire_with for item in closures.get(name, ())}
    refused = []
    dispositions = []
    for item in inventory:
        if item.path in retiring and "retire" in item.permitted_dispositions:
            dispositions.append(
                ClaimTypeDependentDisposition(
                    identity=item.identity,
                    disposition="retire",
                    claim_retirement_reason="was-rescinded"
                    if item.artifact_kind == "claim"
                    else None,
                )
            )
        elif "successor" in item.permitted_dispositions:
            dispositions.append(
                ClaimTypeDependentDisposition(identity=item.identity, disposition="successor")
            )
        else:
            refused.append(
                f"{item.identity.qualified} cannot be carried to "
                f"{item.triggering_identity.qualified}"
            )
    if refused:
        return dict(diff.writes), refused
    try:
        settled, _normalized = build_dependent_closure_candidate(
            tree=tree, changed=changed, inventory=inventory, dispositions=tuple(dispositions)
        )
    except ClaimTypeMigrationError as error:
        return dict(diff.writes), [str(error)]
    diff.plan[:] = [
        item.model_copy(update={"dependent_count": counts[item.identity]})
        if item.identity in counts and item.identity not in diff.semantic_counts
        else item
        for item in diff.plan
    ]
    return {**settled, **diff.writes}, []


def _held_by_other_kits(
    instance: PlaybillInstance, tree: Mapping[str, bytes], kit_id: str
) -> dict[str, str]:
    """Every definition path another installed kit holds -> that kit.

    A kit holds what its receipt records, not only what its current prefixes
    name: a definition the release dropped but the consumer kept stays that
    kit's even when a later release narrows its prefixes past it.
    """

    return {
        entry.path: receipt.kit_id
        for receipt in _receipts(instance, tree)
        if receipt.kit_id != kit_id
        for entry in receipt.artifacts
    }


def _foreign_takeovers(
    instance: PlaybillInstance, tree: Mapping[str, bytes], kit_id: str, diff: _Diff
) -> list[str]:
    """A release may not change a definition another installed kit owns or holds: a
    hard block, like overlapping ownership."""

    owners = [
        (prefix, receipt.kit_id)
        for receipt in _receipts(instance, tree)
        if receipt.kit_id != kit_id and receipt.artifacts
        for prefix in receipt.owns
    ]
    held = _held_by_other_kits(instance, tree, kit_id)
    found: list[str] = []
    for item in diff.plan:
        if item.action != "replace" or item.identity is None:
            continue
        name = item.identity.partition(":")[2]
        owned_by = [owner for prefix, owner in owners if name.startswith(prefix)]
        found.extend(f"{item.identity} is owned by kit {owner}" for owner in owned_by)
        holder = held.get(item.path)
        if holder is not None and holder not in owned_by:
            found.append(f"{item.identity} is held by kit {holder}")
    return found


def _ownership_conflicts(
    instance: PlaybillInstance, tree: Mapping[str, bytes], manifest: KitManifest
) -> list[str]:
    conflicts = []
    for receipt in _receipts(instance, tree):
        if receipt.kit_id == manifest.kit_id or not receipt.artifacts:
            continue
        for prefix in manifest.owns:
            for theirs in receipt.owns:
                if prefix.startswith(theirs) or theirs.startswith(prefix):
                    conflicts.append(f"{prefix} overlaps {theirs}, owned by kit {receipt.kit_id}")
    # A prefix may not take in a definition another kit still holds outside
    # its own prefixes (one it kept after its release dropped it).
    for path, holder in sorted(_held_by_other_kits(instance, tree, manifest.kit_id).items()):
        current = tree.get(path)
        if current is None:
            continue
        identity = _artifact_state(path, current).identity
        if any(identity.name.startswith(prefix) for prefix in manifest.owns) and not any(
            conflict.endswith(f"owned by kit {holder}") for conflict in conflicts
        ):
            conflicts.append(f"{identity.qualified} is held by kit {holder}")
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
) -> KitChangeResult:
    assert mode.head is not None
    base = mode.head
    described = {
        "transition": transition,
        "installed_version": installed_version,
        "provenance": provenance,
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
    pending = next(
        (
            item
            for item in service_list_playbill_proposals(instance, status="open").entries
            if item.target_ref == target
        ),
        None,
    )
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
    admitted = admit_change_set(
        instance,
        mode,
        actor_id=actor_id,
        request=ProposalAdmissionRequest(target_ref=target, proposed_base_oid=base.git_oid),
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
        activation = service_activate_playbill_proposal(
            instance, proposal_id=admitted.proposal_id, activated_by=actor_id
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


def service_add_kit(
    instance: PlaybillInstance,
    request: KitAddRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> KitChangeResult:
    """Propose the diff that brings this instance to one kit release.

    Derived across many artifacts, so it previews unless ``dry_run`` is false.
    """

    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="cruxible.kit.add",
        describe=f"installing kit {request.bundle.manifest.kit_id}",
    ) as mode:
        return _add_kit(instance, mode, request, actor_id=actor_id, timestamp=timestamp)


def _add_kit(
    instance: PlaybillInstance,
    mode: ChangeMode,
    request: KitAddRequest,
    *,
    actor_id: str,
    timestamp: str,
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
    described = {
        "transition": transition,
        "installed_version": installed_version,
        "provenance": manifest.provenance,
    }
    blocked = _ownership_conflicts(instance, tree, manifest)
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
    )
    retire_with = _settle_dropped(
        tree,
        installed,
        owned,
        diff,
        keep=keep,
        retire_dependents=frozenset(request.retire_dependents),
        kept=kept,
    )
    blocked.extend(_foreign_takeovers(instance, tree, manifest.kit_id, diff))
    writes, refused = _settle_dependents(tree, diff, retire_with=retire_with)
    blocked.extend(refused)
    plan = tuple(sorted(diff.plan, key=lambda item: item.path))
    if blocked:
        return KitChangeResult(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="would_block" if mode.previewing else "blocked",
            plan=plan,
            detail="; ".join(blocked),
            coordinate=mode.coordinate,
            **described,  # type: ignore[arg-type]
        )

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
    """Propose retiring every definition a kit owns; carried ones stay.

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
    for entry in receipt.artifacts:
        current = tree.get(entry.path)
        if current is None:
            continue
        state = _artifact_state(entry.path, current)
        if state.lifecycle.state != "live":
            continue
        if _installed_content(entry, tree) != kit_content_digest(json.loads(current)):
            edited.append(entry.path)
            continue
        diff.plan.append(
            KitPathPlan(path=entry.path, action="retire", identity=state.identity.qualified)
        )
        diff.writes[entry.path] = _retirement(entry.path, current)
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
        receipt=receipt.model_copy(update={"artifacts": (), "carried": (), "kept": ()}),
        previous_receipt=shell,
        writes=diff.writes,
        plan=plan,
        actor_id=actor_id,
        timestamp=timestamp,
    )


def service_kit_status(instance: PlaybillInstance) -> KitStatus:
    """Installed kits with the paths edited since install (by content, lineage apart)."""

    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    kits = []
    for receipt in _receipts(instance, tree):
        if not receipt.artifacts:
            continue
        drifted = tuple(
            entry.path
            for entry in receipt.artifacts
            if entry.path not in tree
            or _installed_content(entry, tree) != kit_content_digest(json.loads(tree[entry.path]))
        )
        kits.append(
            InstalledKit(
                kit_id=receipt.kit_id,
                version=receipt.version,
                content_digest=receipt.content_digest,
                source=receipt.source,
                drifted=drifted,
                provenance=receipt.provenance,
                kept=receipt.kept,
            )
        )
    return KitStatus(kits=tuple(kits))
