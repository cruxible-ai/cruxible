"""Kits: export owned definitions as a release, and import one as a governed change set.

A kit's bytes are the bytes an instance accepts. Every artifact digest covers the
artifact's lifecycle, and a lifecycle names its predecessor, so the lineage inside
a kit must be the kit's own release lineage rather than the history of whichever
instance built it. ``build`` re-derives that lineage against the previous release
and rewrites every pin the re-derivation moves; ``add`` then needs no lineage
logic at all, because each artifact law already demands that a successor name the
exact digest it replaces. An instance that edited a kit artifact locally holds a
different digest, so the law refuses the upgrade of that path, and the plan names
it as a conflict before anything is proposed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

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
    InstalledKitV1,
    KitArtifactBytesV1,
    KitArtifactV1,
    KitBundleV1,
    KitManifestV1,
    KitPathPlanV1,
    KitReceiptV1,
    KitReleaseRefV1,
    PlaybillKitAddRequestV1,
    PlaybillKitBuildRequestV1,
    PlaybillKitBuildResultV1,
    PlaybillKitChangeResultV1,
    PlaybillKitRemoveRequestV1,
    PlaybillKitStatusV1,
    kit_artifact_path_allowed,
    kit_receipt_document_id,
)
from cruxible_core.claims.closure import ArtifactDependencyStateV1, parse_dependency_artifact
from cruxible_core.errors import DataValidationError
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
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


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _without_lifecycle(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "lifecycle"}


# Author-controlled literal data; a digest string inside it is a value, never a pin.
_LITERAL_KEYS = frozenset({"lifecycle", "literal_schema", "value_schema"})


def _is_reference(key: str | None) -> bool:
    return key is not None and (key == "artifact_digest" or key.endswith(("_digest", "_digests")))


def _references(value: object, key: str | None = None) -> Iterator[str]:
    """Digest strings held in reference fields, outside lifecycle and literal data."""

    if isinstance(value, str):
        if _is_reference(key):
            yield value
    elif isinstance(value, Mapping):
        for name, item in value.items():
            if name not in _LITERAL_KEYS:
                yield from _references(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item, key)


def _substitute(value: object, remap: Mapping[str, str], key: str | None = None) -> object:
    """Move reference fields from rewritten digests to their new ones; leave literals alone."""

    if isinstance(value, str):
        return remap.get(value, value) if _is_reference(key) else value
    if isinstance(value, Mapping):
        return {
            name: item if name in _LITERAL_KEYS else _substitute(item, remap, name)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_substitute(item, remap, key) for item in value]
    return value


def _artifact_state(path: str, content: bytes) -> ArtifactDependencyStateV1:
    state = parse_dependency_artifact(path, content)
    if state is None:
        raise DataValidationError(f"{path} is not a definition artifact")
    if pretty_canonical_bytes(json.loads(content)) != content:
        raise DataValidationError(f"{path} is not in canonical form")
    return state


def _owned(state: ArtifactDependencyStateV1, owns: tuple[str, ...]) -> bool:
    return state.identity.name.startswith(owns)


class _DefinitionIndex:
    """Every definition artifact in one tree, by path and by digest."""

    def __init__(self, tree: Mapping[str, bytes]) -> None:
        self.tree = tree
        self.states: dict[str, ArtifactDependencyStateV1] = {}
        for path in tree:
            if path.endswith(".json") and path.startswith(_INDEXED_PREFIXES):
                state = parse_dependency_artifact(path, tree[path])
                if state is not None:
                    self.states[path] = state
        self.by_digest = {state.artifact_digest: path for path, state in self.states.items()}
        self.by_identity = {
            (state.identity.kind, state.identity.name): path for path, state in self.states.items()
        }

    def dependencies(self, path: str) -> tuple[str, ...]:
        """Paths this artifact pins, by declared pin or by any digest it carries."""

        state = self.states[path]
        found = set()
        for pin in state.pins:
            target = self.by_digest.get(pin.artifact_digest) or self.by_identity.get(
                (pin.target.kind, pin.target.name)
            )
            if target is not None:
                found.add(target)
        for text in _references(json.loads(self.tree[path])):
            target = self.by_digest.get(text)
            if target is not None:
                found.add(target)
        found.discard(path)
        return tuple(sorted(found))


def _selected(index: _DefinitionIndex, owns: tuple[str, ...]) -> tuple[str, ...]:
    """Owned live definitions plus every definition they pin, in dependency order."""

    roots = [
        path
        for path, state in sorted(index.states.items())
        if kit_artifact_path_allowed(path)
        and state.lifecycle.state == "live"
        and _owned(state, owns)
    ]
    if not roots:
        raise DataValidationError(f"no live definitions are named under {', '.join(owns)}")
    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(path: str, pinned_by: str | None) -> None:
        if path in ordered:
            return
        if path in visiting:
            raise DataValidationError(f"definitions pin each other in a cycle through {path}")
        if not kit_artifact_path_allowed(path):
            raise DataValidationError(
                f"{pinned_by} pins {path}, which a kit cannot carry; "
                "a kit holds definitions, never authority, bindings or state"
            )
        if index.states[path].lifecycle.state != "live":
            raise DataValidationError(f"{pinned_by} pins retired {path}")
        visiting.add(path)
        for dependency in index.dependencies(path):
            visit(dependency, path)
        visiting.discard(path)
        ordered.append(path)

    for root in roots:
        visit(root, None)
    return tuple(ordered)


def service_build_kit(
    instance: PlaybillInstance, request: PlaybillKitBuildRequestV1
) -> PlaybillKitBuildResultV1:
    """Export owned definitions at the accepted head as one release of ``kit_id``."""

    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    installed = {
        path: receipt.kit_id
        for receipt in _receipts(instance, tree)
        if receipt.kit_id != request.kit_id
        for path in receipt.digests()
    }
    return PlaybillKitBuildResultV1(bundle=build_kit(tree, request, installed=installed))


def build_kit(
    tree: Mapping[str, bytes],
    request: PlaybillKitBuildRequestV1,
    *,
    installed: Mapping[str, str] | None = None,
) -> KitBundleV1:
    """Export the owned definitions of one accepted tree as a kit release.

    Owned definitions get the kit's own release lineage. Carried definitions --
    everything owned definitions pin that the kit does not own -- keep their
    exact accepted bytes, so they stay identical to whatever defines them
    (another kit, recorded in ``installed``, or a definition with no history).
    """

    previous = request.previous
    previous_bytes: dict[str, bytes] = {}
    previous_ref = None
    if previous is not None:
        _verify_bundle(previous)
        if previous.manifest.kit_id != request.kit_id:
            raise DataValidationError("the previous release belongs to a different kit")
        if _version_key(previous.manifest.version) >= _version_key(request.version):
            raise DataValidationError("a release version must follow its previous release")
        previous_bytes = previous.contents()
        previous_ref = KitReleaseRefV1(
            version=previous.manifest.version, content_digest=previous.manifest.content_digest
        )
    index = _DefinitionIndex(tree)
    remap: dict[str, str] = {}
    built: dict[str, bytes] = {}
    installed = installed or {}
    for path in _selected(index, request.owns):
        state = index.states[path]
        if not _owned(state, request.owns):
            moved = [
                dep
                for dep in index.dependencies(path)
                if index.states[dep].artifact_digest in remap
            ]
            if moved:
                raise DataValidationError(
                    f"carried {path} pins owned {moved[0]}; own it too so its pins can move"
                )
            if state.lifecycle.predecessor_digest is not None and path not in installed:
                raise DataValidationError(
                    f"carried {path} has local history no consumer can reproduce; own it, or "
                    "install the kit that defines it and build against that"
                )
            built[path] = tree[path]
            continue
        payload = _substitute(json.loads(tree[path]), remap)
        assert isinstance(payload, dict)
        prior = previous_bytes.get(path)
        if prior is not None and _without_lifecycle(json.loads(prior)) == _without_lifecycle(
            payload
        ):
            content = prior
        else:
            predecessor = None if prior is None else _artifact_state(path, prior).artifact_digest
            payload["lifecycle"] = {"predecessor_digest": predecessor, "state": "live"}
            content = pretty_canonical_bytes(payload)
        digest = _artifact_state(path, content).artifact_digest
        if digest != state.artifact_digest:
            remap[state.artifact_digest] = digest
        built[path] = content
    manifest = KitManifestV1(
        kit_id=request.kit_id,
        version=request.version,
        owns=request.owns,
        previous=previous_ref,
        artifacts=tuple(
            KitArtifactV1(
                path=path, artifact_digest=_artifact_state(path, built[path]).artifact_digest
            )
            for path in sorted(built)
        ),
    )
    return KitBundleV1(
        manifest=manifest,
        artifacts=tuple(KitArtifactBytesV1.of(path, built[path]) for path in sorted(built)),
    )


def _verify_bundle(bundle: KitBundleV1) -> dict[str, bytes]:
    contents = bundle.contents()
    digests = bundle.manifest.digests()
    for path, content in contents.items():
        state = _artifact_state(path, content)
        if state.artifact_digest != digests[path]:
            raise DataValidationError(f"{path} does not match its manifest digest")
        if state.lifecycle.state != "live":
            raise DataValidationError(f"{path} is retired; a kit release carries live definitions")
    return contents


def _receipt_at(
    instance: PlaybillInstance, path: str, raw: bytes, kit_id: str
) -> tuple[DocumentShell, KitReceiptV1] | None:
    """The receipt at ``path``, or None when an ordinary Document holds that name."""

    shell = parse_document(raw, path=path)
    if shell.document_kind != KIT_RECEIPT_DOCUMENT_KIND:
        return None
    body = instance.body_store().read(shell.body_digest, access=_RECEIPT_ACCESS)
    receipt = KitReceiptV1.model_validate_json(body)
    if receipt.kit_id != kit_id:
        raise ProposalIntegrityError(f"{path} records kit {receipt.kit_id}, not {kit_id}")
    return shell, receipt


def _read_receipt(
    instance: PlaybillInstance, tree: Mapping[str, bytes], kit_id: str
) -> tuple[DocumentShell, KitReceiptV1] | None:
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


def _receipts(instance: PlaybillInstance, tree: Mapping[str, bytes]) -> Iterator[KitReceiptV1]:
    for path in sorted(tree):
        if path.startswith("documents/kit-") and path.endswith(".json"):
            kit_id = path.removeprefix("documents/kit-").removesuffix(".json")
            found = _receipt_at(instance, path, tree[path], kit_id)
            if found is not None:
                yield found[1]


def _retired(path: str, content: bytes) -> bytes:
    payload = json.loads(content)
    payload["lifecycle"] = {
        "predecessor_digest": _artifact_state(path, content).artifact_digest,
        "state": "retired",
    }
    return pretty_canonical_bytes(payload)


def _plan(
    tree: Mapping[str, bytes],
    installed: Mapping[str, str],
    incoming: Mapping[str, bytes],
    carried: Mapping[str, bytes] | None = None,
) -> tuple[list[KitPathPlanV1], dict[str, bytes]]:
    """Per-path actions and the bytes they write; a conflict writes nothing.

    ``installed`` and ``incoming`` are the kit's owned definitions, which it may
    add, replace and retire. ``carried`` definitions belong to someone else: the
    kit may add one that is absent, and otherwise needs it exactly as accepted.
    """

    carried = carried or {}
    plan: list[KitPathPlanV1] = []
    writes: dict[str, bytes] = {}
    for path, content in sorted(carried.items()):
        current = tree.get(path)
        state = _artifact_state(path, content)
        if current is None and state.lifecycle.predecessor_digest is None:
            plan.append(KitPathPlanV1(path=path, action="add", detail="carried"))
            writes[path] = content
        elif current is None:
            plan.append(
                KitPathPlanV1(
                    path=path,
                    action="conflict",
                    detail="carried; install the release that defines it first",
                )
            )
        elif _artifact_state(path, current).artifact_digest == state.artifact_digest:
            plan.append(KitPathPlanV1(path=path, action="unchanged", detail="carried"))
        else:
            plan.append(
                KitPathPlanV1(
                    path=path, action="conflict", detail="carried; differs from the accepted one"
                )
            )
    for path in sorted((set(installed) | set(incoming)) - set(carried)):
        current = tree.get(path)
        current_digest = None if current is None else _artifact_state(path, current).artifact_digest
        if path in incoming:
            new = incoming[path]
            new_digest = _artifact_state(path, new).artifact_digest
            if current_digest == new_digest:
                plan.append(KitPathPlanV1(path=path, action="unchanged"))
            elif current is None:
                plan.append(KitPathPlanV1(path=path, action="add"))
                writes[path] = new
            elif installed.get(path) == current_digest:
                plan.append(KitPathPlanV1(path=path, action="replace"))
                writes[path] = new
            else:
                detail = (
                    "edited since the kit installed it"
                    if path in installed
                    else "already defined outside this kit"
                )
                plan.append(KitPathPlanV1(path=path, action="conflict", detail=detail))
        elif current is None:
            continue
        elif installed[path] != current_digest:
            plan.append(
                KitPathPlanV1(
                    path=path, action="conflict", detail="edited since the kit installed it"
                )
            )
        elif _artifact_state(path, current).lifecycle.state == "live":
            plan.append(KitPathPlanV1(path=path, action="retire"))
            writes[path] = _retired(path, current)
    return plan, writes


def _ownership_conflicts(
    instance: PlaybillInstance, tree: Mapping[str, bytes], manifest: KitManifestV1
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


def _missing_interfaces(
    tree: Mapping[str, bytes], incoming: Mapping[str, bytes]
) -> tuple[str, ...]:
    providers = [tree[path] for path in tree if path.startswith("providers/")]
    missing = []
    for path, content in sorted(incoming.items()):
        if not path.startswith("provider-interfaces/"):
            continue
        state = _artifact_state(path, content)
        if not any(state.artifact_digest.encode("ascii") in provider for provider in providers):
            missing.append(state.identity.name)
    return tuple(missing)


def _submit(
    instance: PlaybillInstance,
    *,
    kit_id: str,
    version: str | None,
    receipt: KitReceiptV1,
    previous_receipt: DocumentShell | None,
    writes: Mapping[str, bytes],
    plan: tuple[KitPathPlanV1, ...],
    actor_id: str,
    timestamp: str,
    missing_interfaces: tuple[str, ...] = (),
) -> PlaybillKitChangeResultV1:
    base = instance.accepted_coordinate()
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
    if pending is None:
        submitted = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id=actor_id),
            request=ProposalAdmissionRequest(target_ref=target, proposed_base_oid=base.git_oid),
            candidate_tree=candidate,
            timestamp=timestamp,
        )
        if (
            submitted.evaluation.verdict != "candidate"
            or submitted.evaluation.candidate_digest is None
        ):
            return PlaybillKitChangeResultV1(
                kit_id=kit_id,
                version=version,
                status="blocked",
                proposal_id=submitted.admission.proposal_id,
                plan=plan,
                missing_interfaces=missing_interfaces,
                detail="Refused: "
                + "; ".join(item.code for item in submitted.evaluation.diagnostics),
            )
        proposal_id: str = submitted.admission.proposal_id
        candidate_digest: str | None = submitted.evaluation.candidate_digest
    else:
        proposal_id, candidate_digest = pending.proposal_id, pending.candidate_digest
    assert candidate_digest is not None
    evidence = instance.proposal_evidence().read_candidate(candidate_digest)
    return PlaybillKitChangeResultV1(
        kit_id=kit_id,
        version=version,
        status="proposed",
        proposal_id=proposal_id,
        approval_required=bool(evidence.approval_requirements),
        plan=plan,
        missing_interfaces=missing_interfaces,
    )


def service_add_kit(
    instance: PlaybillInstance,
    request: PlaybillKitAddRequestV1,
    *,
    actor_id: str,
    timestamp: str,
) -> PlaybillKitChangeResultV1:
    """Propose installing or upgrading a kit release as one change set."""

    bundle = request.bundle
    manifest = bundle.manifest
    contents = _verify_bundle(bundle)
    owned_paths = {
        path
        for path, content in contents.items()
        if _owned(_artifact_state(path, content), manifest.owns)
    }
    incoming = {path: contents[path] for path in owned_paths}
    carried = {path: content for path, content in contents.items() if path not in owned_paths}
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    found = _read_receipt(instance, tree, manifest.kit_id)
    shell, installed = (None, None) if found is None else found
    blocked = _ownership_conflicts(instance, tree, manifest)
    if installed is not None and installed.artifacts:
        if installed.content_digest == manifest.content_digest:
            return PlaybillKitChangeResultV1(
                kit_id=manifest.kit_id, version=manifest.version, status="unchanged"
            )
        if (
            manifest.previous is None
            or manifest.previous.content_digest != installed.content_digest
        ):
            blocked.append(
                f"release {manifest.version} does not follow the installed {installed.version}; "
                "upgrade one release at a time"
            )
    plan, writes = _plan(tree, {} if installed is None else installed.digests(), incoming, carried)
    conflicts = [item.path for item in plan if item.action == "conflict"]
    if conflicts:
        blocked.append(f"{len(conflicts)} path(s) conflict")
    missing = _missing_interfaces(tree, contents)
    if blocked:
        return PlaybillKitChangeResultV1(
            kit_id=manifest.kit_id,
            version=manifest.version,
            status="blocked",
            plan=tuple(plan),
            missing_interfaces=missing,
            detail="; ".join(blocked),
        )
    receipt = KitReceiptV1(
        kit_id=manifest.kit_id,
        version=manifest.version,
        content_digest=manifest.content_digest,
        owns=manifest.owns,
        artifacts=tuple(item for item in manifest.artifacts if item.path in owned_paths),
        carried=tuple(item for item in manifest.artifacts if item.path not in owned_paths),
        source=request.source,
    )
    return _submit(
        instance,
        kit_id=manifest.kit_id,
        version=manifest.version,
        receipt=receipt,
        previous_receipt=shell,
        writes=writes,
        plan=tuple(plan),
        actor_id=actor_id,
        timestamp=timestamp,
        missing_interfaces=missing,
    )


def service_remove_kit(
    instance: PlaybillInstance,
    request: PlaybillKitRemoveRequestV1,
    *,
    actor_id: str,
    timestamp: str,
) -> PlaybillKitChangeResultV1:
    """Propose retiring every artifact a kit installed; its receipt records the removal."""

    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    found = _read_receipt(instance, tree, request.kit_id)
    if found is None or not found[1].artifacts:
        return PlaybillKitChangeResultV1(kit_id=request.kit_id, version=None, status="unchanged")
    shell, installed = found
    plan, writes = _plan(tree, installed.digests(), {})
    if any(item.action == "conflict" for item in plan):
        return PlaybillKitChangeResultV1(
            kit_id=request.kit_id,
            version=installed.version,
            status="blocked",
            plan=tuple(plan),
            detail="edited kit paths must be reverted or retired by their own change first",
        )
    receipt = installed.model_copy(update={"artifacts": (), "carried": ()})
    return _submit(
        instance,
        kit_id=request.kit_id,
        version=installed.version,
        receipt=receipt,
        previous_receipt=shell,
        writes=writes,
        plan=tuple(plan),
        actor_id=actor_id,
        timestamp=timestamp,
    )


def service_kit_status(instance: PlaybillInstance) -> PlaybillKitStatusV1:
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
            InstalledKitV1(
                kit_id=receipt.kit_id,
                version=receipt.version,
                content_digest=receipt.content_digest,
                source=receipt.source,
                drifted=drifted,
            )
        )
    return PlaybillKitStatusV1(kits=tuple(kits))
