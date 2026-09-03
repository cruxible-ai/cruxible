"""Ordinary governed proposal orchestration for compiler-shipped Provider seeds."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

from cruxible_client import contracts
from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.canonical import canonical_digest, is_candidate_card_path
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.provider_interfaces import (
    parse_provider_interface,
    provider_interface_digest,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    parse_provider,
    provider_digest,
    provider_path,
    render_provider,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_ENTRYPOINT,
    WORKSPACE_FILE_INTERFACE_ID,
    WORKSPACE_FILE_PROVIDER_ID,
    WORKSPACE_FILE_SEED_MANIFEST,
    workspace_file_interface_registration,
    workspace_file_provider,
)
from cruxible_core.playbill.service.documents import service_activate_playbill_proposal
from cruxible_core.runtime.provider_runtime import ProviderSeedMaterializationConfigV1


def _without_lifecycle(value: Any) -> dict[str, object]:
    return cast(dict[str, object], value.model_dump(mode="json", exclude={"lifecycle"}))


def _seed_candidate_tree(
    instance: PlaybillInstance,
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    candidate_tree = dict(tree)
    changed: list[str] = []

    interface_path = provider_interface_path(WORKSPACE_FILE_INTERFACE_ID)
    desired_interface = workspace_file_interface_registration()
    current_interface_bytes = tree.get(interface_path)
    if current_interface_bytes is not None:
        current_interface = parse_provider_interface(
            current_interface_bytes,
            path=interface_path,
        )
        if current_interface.lifecycle.state != "live":
            raise ProposalIntegrityError(
                "workspace.file seed is retired; propose an explicit successor or restore it"
            )
        if _without_lifecycle(current_interface) == _without_lifecycle(desired_interface):
            desired_interface = current_interface
        else:
            desired_interface = workspace_file_interface_registration(
                lifecycle=ArtifactLifecycle(
                    predecessor_digest=provider_interface_digest(current_interface).tagged
                )
            )
    desired_interface_bytes = render_provider_interface(desired_interface)
    if desired_interface_bytes != current_interface_bytes:
        candidate_tree[interface_path] = desired_interface_bytes
        changed.append(interface_path)

    provider_artifact_path = provider_path(WORKSPACE_FILE_PROVIDER_ID)
    desired_provider = workspace_file_provider(
        interface_artifact_digest=provider_interface_digest(desired_interface).tagged,
    )
    current_provider_bytes = tree.get(provider_artifact_path)
    if current_provider_bytes is not None:
        current_provider = parse_provider(current_provider_bytes, path=provider_artifact_path)
        if current_provider.lifecycle.state != "live":
            raise ProposalIntegrityError(
                "workspace.file Provider is retired; propose an explicit successor or restore it"
            )
        if _without_lifecycle(current_provider) == _without_lifecycle(desired_provider):
            desired_provider = cast(Any, current_provider)
        else:
            desired_provider = workspace_file_provider(
                interface_artifact_digest=provider_interface_digest(desired_interface).tagged,
                lifecycle=ArtifactLifecycle(
                    predecessor_digest=provider_digest(current_provider).tagged
                ),
            )
    desired_provider_bytes = render_provider(desired_provider)
    if desired_provider_bytes != current_provider_bytes:
        candidate_tree[provider_artifact_path] = desired_provider_bytes
        changed.append(provider_artifact_path)

    return candidate_tree, tuple(sorted(changed, key=lambda path: path.encode("utf-8")))


def _public_coordinate(instance: PlaybillInstance) -> contracts.PlaybillAcceptedCoordinate:
    return contracts.PlaybillAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )


def _git(checkout: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(checkout), *arguments),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProposalIntegrityError(
            "workspace.file seed checkout cannot reproduce its local materialization"
        ) from exc
    return completed.stdout.strip()


@lru_cache(maxsize=8)
def _derive_local_seed_pins(checkout_text: str, provider_commit: str) -> dict[str, Any]:
    """Build the pinned wheel and run the provider repository's authoritative derivation."""

    checkout = Path(checkout_text)
    uv = shutil.which("uv")
    if uv is None:
        raise ProposalIntegrityError("workspace.file local materialization requires uv")
    package = checkout / "packages" / "cruxible-provider-workspace"
    script = checkout / "scripts" / "seed_pins.py"
    try:
        with tempfile.TemporaryDirectory(prefix="cruxible-workspace-seed-") as temporary:
            output = Path(temporary)
            subprocess.run(
                (uv, "build", "--wheel", "--offline", "--out-dir", str(output), str(package)),
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            wheels = tuple(output.glob("*.whl"))
            if len(wheels) != 1:
                raise ProposalIntegrityError(
                    "workspace.file checkout did not build exactly one wheel"
                )
            completed = subprocess.run(
                (
                    sys.executable,
                    str(script),
                    "--repo",
                    str(checkout),
                    "--package",
                    "cruxible-provider-workspace",
                    "--wheel",
                    str(wheels[0]),
                    "--json",
                ),
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            pins = json.loads(completed.stdout)
    except ProposalIntegrityError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ProposalIntegrityError(
            "workspace.file seed checkout cannot reproduce its local materialization"
        ) from exc
    if not isinstance(pins, dict):
        raise ProposalIntegrityError("workspace.file seed derivation returned malformed pins")
    # The cache key documents the exact immutable checkout identity used to derive these pins.
    if provider_commit != WORKSPACE_FILE_SEED_MANIFEST.provider_commit:
        raise ProposalIntegrityError("workspace.file seed checkout commit is not pinned")
    return cast(dict[str, Any], pins)


def _validate_local_materialization(
    configured: ProviderSeedMaterializationConfigV1 | None,
) -> None:
    """Check configured custody without allowing its host path into authority bytes."""

    if configured is None:
        raise ProposalIntegrityError(
            "workspace.file local seed requires provider runtime config seed_materializations"
        )
    if configured.provider_id != WORKSPACE_FILE_PROVIDER_ID:
        raise ProposalIntegrityError("workspace.file seed materialization names another Provider")
    expected_materializations = dict(WORKSPACE_FILE_SEED_MANIFEST.materialization_digests)
    if (
        configured.provider_commit != WORKSPACE_FILE_SEED_MANIFEST.provider_commit
        or expected_materializations.get(configured.environment_pin_key)
        != configured.materialization_digest
    ):
        raise ProposalIntegrityError(
            "workspace.file local materialization differs from the compiler-owned seed pins"
        )
    try:
        checkout = Path(configured.checkout_path).resolve(strict=True)
    except OSError as exc:
        raise ProposalIntegrityError(
            "workspace.file seed checkout cannot reproduce its local materialization"
        ) from exc
    if str(checkout) != configured.checkout_path or not checkout.is_dir():
        raise ProposalIntegrityError("workspace.file seed checkout is not one canonical directory")
    if _git(checkout, "rev-parse", "HEAD") != WORKSPACE_FILE_SEED_MANIFEST.provider_commit:
        raise ProposalIntegrityError(
            "workspace.file seed checkout commit or lock differs from the pinned adapter"
        )
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise ProposalIntegrityError("workspace.file seed checkout must be clean")
    lock_path = checkout / "packages" / "cruxible-provider-workspace" / "uv.lock"
    try:
        lock_digest = f"sha256:{hashlib.sha256(lock_path.read_bytes()).hexdigest()}"
        pins = _derive_local_seed_pins(str(checkout), configured.provider_commit)
    except OSError as exc:
        raise ProposalIntegrityError(
            "workspace.file seed checkout cannot reproduce its local materialization"
        ) from exc
    implementations = pins.get("implementations")
    implementation = (
        implementations[0] if isinstance(implementations, list) and implementations else {}
    )
    distribution = pins.get("distribution")
    derived_materializations = pins.get("materialization_digests")
    if (
        lock_digest != WORKSPACE_FILE_SEED_MANIFEST.lock_digest
        or pins.get("lock_sha256") != WORKSPACE_FILE_SEED_MANIFEST.lock_digest
        or not isinstance(distribution, dict)
        or distribution.get("sha256") != WORKSPACE_FILE_SEED_MANIFEST.wheel_digest
        or not isinstance(implementation, dict)
        or implementation.get("interface_id") != WORKSPACE_FILE_INTERFACE_ID
        or implementation.get("interface_digest")
        != workspace_file_interface_registration().interface_digest
        or implementation.get("entrypoint") != WORKSPACE_FILE_ENTRYPOINT
        or implementation.get("implementation_digest")
        != WORKSPACE_FILE_SEED_MANIFEST.implementation_digest
        or derived_materializations != dict(WORKSPACE_FILE_SEED_MANIFEST.materialization_digests)
    ):
        raise ProposalIntegrityError(
            "workspace.file seed checkout commit or lock differs from the pinned adapter"
        )
    if _git(checkout, "rev-parse", "HEAD") != configured.provider_commit or _git(
        checkout, "status", "--porcelain", "--untracked-files=all"
    ):
        raise ProposalIntegrityError("workspace.file seed checkout changed during validation")


def _pending_seed(
    instance: PlaybillInstance,
    *,
    candidate_tree: dict[str, bytes],
    changed_paths: tuple[str, ...],
) -> contracts.PlaybillProviderSeedResultV1 | None:
    coordinate = instance.accepted_coordinate()
    accepted_candidates = {
        generation.record.candidate_digest
        for generation in instance.accepted_history()
        if generation.record is not None
    }
    evidence = instance.proposal_evidence()
    for admission in evidence.list_admissions():
        evaluation = evidence.read_evaluation(admission.proposal_id)
        if (
            evaluation.verdict != "candidate"
            or evaluation.candidate_digest is None
            or evaluation.evaluated_tree_oid is None
            or evaluation.candidate_digest in accepted_candidates
        ):
            continue
        candidate = evidence.read_candidate(evaluation.candidate_digest)
        # The evaluated tree carries the derivative cards evaluation derives;
        # they are not authored members, so comparing them would make every
        # pending seed look different from the one this call would submit.
        evaluated = {
            path: content
            for path, content in instance.proposal_tree(evaluation.evaluated_tree_oid).items()
            if not is_candidate_card_path(path)
        }
        if (
            candidate.candidate.parent_semantic_root != coordinate.semantic_root
            or evaluated != candidate_tree
        ):
            continue
        return contracts.PlaybillProviderSeedResultV1(
            provider_id=WORKSPACE_FILE_PROVIDER_ID,
            materialization_source=WORKSPACE_FILE_SEED_MANIFEST.materialization_source,
            status="pending",
            changed_paths=changed_paths,
            proposal_id=admission.proposal_id,
            candidate_digest=evaluation.candidate_digest,
            approval_required=bool(candidate.approval_requirements),
            accepted_coordinate=_public_coordinate(instance),
        )
    return None


def service_seed_workspace_file_provider(
    instance: PlaybillInstance,
    *,
    actor_id: str,
    timestamp: str,
    configured_materialization: ProviderSeedMaterializationConfigV1 | None = None,
) -> contracts.PlaybillProviderSeedResultV1:
    """Submit the exact seed through ``ProposalService`` and follow normal policy."""

    _validate_local_materialization(configured_materialization)
    candidate_tree, changed_paths = _seed_candidate_tree(instance)
    if not changed_paths:
        return contracts.PlaybillProviderSeedResultV1(
            provider_id=WORKSPACE_FILE_PROVIDER_ID,
            materialization_source=WORKSPACE_FILE_SEED_MANIFEST.materialization_source,
            status="already_current",
            changed_paths=(),
            approval_required=False,
            accepted_coordinate=_public_coordinate(instance),
        )

    pending = _pending_seed(
        instance,
        candidate_tree=candidate_tree,
        changed_paths=changed_paths,
    )
    if pending is not None:
        return pending

    base = instance.accepted_coordinate()
    proposal_suffix = canonical_digest(
        "playbill-workspace-file-seed-target-v1",
        {
            "base_semantic_root": base.semantic_root,
            "members": {
                path: f"sha256:{hashlib.sha256(candidate_tree[path]).hexdigest()}"
                for path in changed_paths
            },
        },
    )
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=(
                f"refs/proposals/{actor_id}/provider-seed-workspace-file-"
                f"{proposal_suffix.removeprefix('sha256:')}"
            ),
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    if result.evaluation.verdict != "candidate" or result.evaluation.candidate_digest is None:
        diagnostics = "; ".join(item.code for item in result.evaluation.diagnostics)
        raise ProposalIntegrityError(
            f"workspace.file Provider seed was refused by ordinary proposal law: {diagnostics}"
        )
    candidate = instance.proposal_evidence().read_candidate(result.evaluation.candidate_digest)
    approval_required = bool(candidate.approval_requirements)
    status: Literal["proposed", "activated", "lost_cas"] = "proposed"
    if not approval_required:
        activation = service_activate_playbill_proposal(
            instance,
            proposal_id=result.admission.proposal_id,
            activated_by=actor_id,
        )
        status = "activated" if activation.status == "accepted" else "lost_cas"
    return contracts.PlaybillProviderSeedResultV1(
        provider_id=WORKSPACE_FILE_PROVIDER_ID,
        materialization_source=WORKSPACE_FILE_SEED_MANIFEST.materialization_source,
        status=status,
        changed_paths=changed_paths,
        proposal_id=result.admission.proposal_id,
        candidate_digest=result.evaluation.candidate_digest,
        approval_required=approval_required,
        accepted_coordinate=_public_coordinate(instance),
    )


__all__ = ["service_seed_workspace_file_provider"]
