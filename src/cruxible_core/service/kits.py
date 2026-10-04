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
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_digest, pretty_canonical_bytes
from cruxible_client.contracts.cas_contracts import BodyAccessContext
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
    KitArtifact,
    KitArtifactBytes,
    KitBundle,
    KitInstalledArtifact,
    KitManifest,
    KitPathPlan,
    KitReceipt,
    PlaybillKitAddRequest,
    PlaybillKitBuildRequest,
    PlaybillKitBuildResult,
    PlaybillKitChangeResult,
    PlaybillKitRemoveRequest,
    PlaybillKitStatus,
    kit_artifact_path_allowed,
    kit_receipt_document_id,
)
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.claims.artifact_references import (
    move_references,
    referenced_digests,
    referenced_identities,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationError,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.claims.closure import ArtifactDependencyStateV1, parse_dependency_artifact
from cruxible_core.errors import DataValidationError, RequestRefusedError
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
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
    instance: PlaybillInstance, request: PlaybillKitBuildRequest
) -> PlaybillKitBuildResult:
    """Export owned definitions at the accepted head as one release of ``kit_id``."""

    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    return PlaybillKitBuildResult(bundle=build_kit(tree, request))


def build_kit(tree: Mapping[str, bytes], request: PlaybillKitBuildRequest) -> KitBundle:
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


def _diff_release(
    tree: Mapping[str, bytes],
    contents: Mapping[str, bytes],
    *,
    owns: tuple[str, ...],
    installed: Mapping[str, str],
) -> tuple[_Diff, set[str]]:
    """Plan every release path against the tree; returns the diff and the owned paths."""

    states = {path: _artifact_state(path, content) for path, content in contents.items()}
    payloads = {path: json.loads(content) for path, content in contents.items()}
    owned = {path for path, state in states.items() if _owned(state, owns)}
    diff = _Diff()
    for path, _pinned in _dependency_order(states, payloads):
        release_digest = states[path].artifact_digest
        payload = _substitute(path, payloads[path], diff.installed)
        carried = path not in owned
        tag = "carried" if carried else None
        current = tree.get(path)
        if current is None:
            content, digest = _render(path, payload)
            diff.plan.append(KitPathPlan(path=path, action="add", detail=tag))
            diff.writes[path] = content
            diff.installed[release_digest] = digest
            continue
        current_state = _artifact_state(path, current)
        if current_state.lifecycle.state != "live":
            diff.plan.append(
                KitPathPlan(path=path, action="conflict", detail="retired in this instance")
            )
            continue
        if _without_lifecycle(json.loads(current)) == _without_lifecycle(payload):
            diff.plan.append(KitPathPlan(path=path, action="unchanged", detail=tag))
            diff.installed[release_digest] = current_state.artifact_digest
            continue
        if carried:
            detail = "carried; differs from the accepted one"
        elif installed.get(path) == current_state.artifact_digest:
            content, digest = _render(
                path,
                _successor_payload(
                    payload, predecessor=current_state.artifact_digest, state="live"
                ),
            )
            diff.plan.append(KitPathPlan(path=path, action="replace"))
            diff.writes[path] = content
            diff.installed[release_digest] = digest
            continue
        elif path in installed:
            detail = "edited since the kit installed it"
        else:
            detail = "already defined outside this kit"
        diff.plan.append(KitPathPlan(path=path, action="conflict", detail=detail))
    return diff, owned


def _retire_dropped(
    tree: Mapping[str, bytes], installed: Mapping[str, str], keep: set[str], diff: _Diff
) -> None:
    """Retire owned paths the kit installed that the release no longer has."""

    for path, installed_digest in sorted(installed.items()):
        if path in keep:
            continue
        current = tree.get(path)
        if current is None:
            continue
        state = _artifact_state(path, current)
        if state.artifact_digest != installed_digest:
            diff.plan.append(
                KitPathPlan(
                    path=path, action="conflict", detail="edited since the kit installed it"
                )
            )
        elif state.lifecycle.state == "live":
            content, _digest = _render(
                path,
                _successor_payload(
                    json.loads(current), predecessor=state.artifact_digest, state="retired"
                ),
            )
            diff.plan.append(KitPathPlan(path=path, action="retire"))
            diff.writes[path] = content


def _settle_dependents(
    tree: Mapping[str, bytes],
    diff: _Diff,
    overrides: Mapping[str, ClaimTypeDependentDisposition],
) -> tuple[dict[str, bytes], list[str]]:
    """Every write, plus one successor for each accepted dependent they change.

    A normal change set owes the closure of what it changes: every live artifact
    pinning a replaced or retired kit definition takes a successor in the same
    generation. Each is read at its accepted bytes, carried (re-pinned) to the
    kit's final definitions by default or retired when the request says so, and
    settled once however many kit definitions it pins. A dependent of a retired
    definition has nothing to be carried to, so it needs an explicit disposition.
    """

    changed = {path: content for path, content in diff.writes.items() if path in tree}
    if not changed:
        return dict(diff.writes), []
    retired = {
        _artifact_state(path, content).identity.qualified
        for path, content in changed.items()
        if _artifact_state(path, content).lifecycle.state == "retired"
    }
    roots = tuple(_artifact_state(path, tree[path]).identity for path in sorted(changed))
    try:
        inventory = dependent_closure_inventory(
            tree, roots=roots, fixed_paths=frozenset(diff.writes)
        )
    except ClaimTypeMigrationError as error:
        return dict(diff.writes), [str(error)]
    if not inventory:
        return dict(diff.writes), []
    refused = []
    dispositions = []
    for item in inventory:
        qualified = item.identity.qualified
        chosen = overrides.get(qualified)
        if chosen is None and (
            item.triggering_identity.qualified not in retired
            and "successor" in item.permitted_dispositions
        ):
            chosen = ClaimTypeDependentDisposition(identity=item.identity, disposition="successor")
        if chosen is None:
            refused.append(f"{qualified} needs a disposition")
            continue
        dispositions.append(chosen)
    if refused:
        return dict(diff.writes), refused
    try:
        settled, normalized = build_dependent_closure_candidate(
            tree=tree, changed=changed, inventory=inventory, dispositions=tuple(dispositions)
        )
    except ClaimTypeMigrationError as error:
        return dict(diff.writes), [str(error)]
    paths = {item.identity.qualified: item.path for item in inventory}
    for outcome in normalized:
        diff.plan.append(
            KitPathPlan(
                path=paths[outcome.identity.qualified],
                action="carry" if outcome.disposition == "successor" else "retire",
                detail="depends on a changed kit definition",
            )
        )
    return {**settled, **diff.writes}, []


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
) -> PlaybillKitChangeResult:
    assert mode.head is not None
    base = mode.head
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
        return PlaybillKitChangeResult(
            kit_id=kit_id,
            version=version,
            status="would_propose" if mode.previewing else "proposed",
            proposal_id=pending.proposal_id,
            approval_required=bool(evidence.approval_requirements),
            plan=plan,
            coordinate=mode.coordinate,
        )
    admitted = admit_change_set(
        instance,
        mode,
        actor_id=actor_id,
        request=ProposalAdmissionRequest(target_ref=target, proposed_base_oid=base.git_oid),
        candidate_tree=candidate,
        timestamp=timestamp,
    )
    return PlaybillKitChangeResult(
        kit_id=kit_id,
        version=version,
        status=admitted.status,
        proposal_id=admitted.proposal_id,
        approval_required=admitted.approval_required,
        plan=plan,
        detail=None if admitted.admitted else admitted.refusal_detail(),
        coordinate=mode.coordinate,
    )


def _overrides(request: PlaybillKitAddRequest) -> dict[str, ClaimTypeDependentDisposition]:
    chosen = {}
    for item in request.dependents:
        disposition = item.disposition
        if disposition != "successor" and disposition != "retire":
            raise DataValidationError(
                f"{item.identity.qualified}: a kit install carries or retires a dependent; "
                f"{disposition} needs its own change set"
            )
        chosen[item.identity.qualified] = ClaimTypeDependentDisposition(
            identity=ArtifactIdentity.model_validate(item.identity.model_dump()),
            disposition=disposition,
            claim_retirement_reason=item.claim_retirement_reason,
            claim_effective_until=item.claim_effective_until,
        )
    return chosen


def service_add_kit(
    instance: PlaybillInstance,
    request: PlaybillKitAddRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> PlaybillKitChangeResult:
    """Propose the diff that brings this instance to one kit release.

    Derived across many artifacts, so it previews unless ``dry_run`` is false.
    """

    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="playbill.kit.add",
        describe=f"installing kit {request.bundle.manifest.kit_id}",
    ) as mode:
        return _add_kit(instance, mode, request, actor_id=actor_id, timestamp=timestamp)


def _add_kit(
    instance: PlaybillInstance,
    mode: ChangeMode,
    request: PlaybillKitAddRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> PlaybillKitChangeResult:
    assert mode.head is not None
    bundle = request.bundle
    manifest = bundle.manifest
    contents = _verify_bundle(bundle)
    overrides = _overrides(request)
    tree = instance.immutable_tree_at(mode.head.git_oid)
    found = _read_receipt(instance, tree, manifest.kit_id)
    shell, receipt = (None, None) if found is None else found
    installed = {} if receipt is None else receipt.digests()
    blocked = _ownership_conflicts(instance, tree, manifest)
    diff, owned = _diff_release(tree, contents, owns=manifest.owns, installed=installed)
    _retire_dropped(tree, installed, owned, diff)
    writes, refused = _settle_dependents(tree, diff, overrides)
    blocked.extend(refused)
    conflicts = [item.path for item in diff.plan if item.action == "conflict"]
    if conflicts:
        blocked.append(f"{len(conflicts)} path(s) conflict")
    plan = tuple(sorted(diff.plan, key=lambda item: item.path))
    if blocked:
        return PlaybillKitChangeResult(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="would_block" if mode.previewing else "blocked",
            plan=plan,
            detail="; ".join(blocked),
            coordinate=mode.coordinate,
        )
    if not writes and receipt is not None and receipt.content_digest == manifest.content_digest:
        return PlaybillKitChangeResult(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="unchanged",
            plan=plan,
            coordinate=mode.coordinate,
        )

    def entries(paths: set[str]) -> tuple[KitInstalledArtifact, ...]:
        return tuple(
            KitInstalledArtifact(
                path=item.path,
                release_digest=item.artifact_digest,
                installed_digest=diff.installed[item.artifact_digest],
            )
            for item in manifest.artifacts
            if item.path in paths
        )

    receipt_body = KitReceipt(
        kit_id=manifest.kit_id,
        version=manifest.version,
        content_digest=manifest.content_digest,
        owns=manifest.owns,
        artifacts=entries(owned),
        carried=entries(set(contents) - owned),
        source=request.source,
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
    )


def service_remove_kit(
    instance: PlaybillInstance,
    request: PlaybillKitRemoveRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> PlaybillKitChangeResult:
    """Propose retiring every definition a kit owns; carried ones stay.

    Derived across many artifacts, so it previews unless ``dry_run`` is false.
    """

    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="derived",
        operation="playbill.kit.remove",
        describe=f"removing kit {request.kit_id}",
    ) as mode:
        return _remove_kit(instance, mode, request, actor_id=actor_id, timestamp=timestamp)


def _remove_kit(
    instance: PlaybillInstance,
    mode: ChangeMode,
    request: PlaybillKitRemoveRequest,
    *,
    actor_id: str,
    timestamp: str,
) -> PlaybillKitChangeResult:
    assert mode.head is not None
    tree = instance.immutable_tree_at(mode.head.git_oid)
    found = _read_receipt(instance, tree, request.kit_id)
    if found is None or not found[1].artifacts:
        installed = sorted(
            receipt.kit_id for receipt in _receipts(instance, tree) if receipt.artifacts
        )
        named = ", ".join(installed) if installed else "none"
        raise RequestRefusedError(
            "playbill.kit.not_installed",
            f"kit {request.kit_id!r} is not installed; installed: {named}",
            repair=RepairOperation(operation="playbill.kit.status"),
        )
    shell, receipt = found
    diff = _Diff()
    _retire_dropped(tree, receipt.digests(), set(), diff)
    plan = tuple(diff.plan)
    if any(item.action == "conflict" for item in plan):
        return PlaybillKitChangeResult(
            kit_id=request.kit_id,
            version=receipt.version,
            status="would_block" if mode.previewing else "blocked",
            plan=plan,
            detail="edited kit paths must be reverted or retired by their own change first",
            coordinate=mode.coordinate,
        )
    return _submit(
        instance,
        mode,
        kit_id=request.kit_id,
        version=receipt.version,
        receipt=receipt.model_copy(update={"artifacts": (), "carried": ()}),
        previous_receipt=shell,
        writes=diff.writes,
        plan=plan,
        actor_id=actor_id,
        timestamp=timestamp,
    )


def service_kit_status(instance: PlaybillInstance) -> PlaybillKitStatus:
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    kits = []
    for receipt in _receipts(instance, tree):
        if not receipt.artifacts:
            continue
        drifted = tuple(
            path
            for path, digest in receipt.digests().items()
            if path not in tree or _artifact_state(path, tree[path]).artifact_digest != digest
        )
        kits.append(
            InstalledKit(
                kit_id=receipt.kit_id,
                version=receipt.version,
                content_digest=receipt.content_digest,
                source=receipt.source,
                drifted=drifted,
            )
        )
    return PlaybillKitStatus(kits=tuple(kits))
