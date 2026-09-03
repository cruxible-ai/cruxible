"""Ordinary governed proposal orchestration for compiler-shipped Provider seeds."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Literal, cast

from cruxible_client import contracts
from cruxible_client.contracts.artifacts import ArtifactLifecycle
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


def _validate_local_materialization(
    configured: ProviderSeedMaterializationConfigV1 | None,
) -> None:
    """Check configured custody without allowing its host path into authority bytes."""

    if configured is None:
        return
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
    checkout = Path(configured.checkout_path).resolve(strict=True)
    if str(checkout) != configured.checkout_path or not checkout.is_dir():
        raise ProposalIntegrityError("workspace.file seed checkout is not one canonical directory")
    lock_path = checkout / "packages" / "cruxible-provider-workspace" / "uv.lock"
    try:
        lock_digest = f"sha256:{hashlib.sha256(lock_path.read_bytes()).hexdigest()}"
        completed = subprocess.run(
            ("git", "-C", str(checkout), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProposalIntegrityError(
            "workspace.file seed checkout cannot reproduce its local materialization"
        ) from exc
    if (
        lock_digest != WORKSPACE_FILE_SEED_MANIFEST.lock_digest
        or completed.stdout.strip() != WORKSPACE_FILE_SEED_MANIFEST.provider_commit
    ):
        raise ProposalIntegrityError(
            "workspace.file seed checkout commit or lock differs from the pinned adapter"
        )


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

    base = instance.accepted_coordinate()
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/provider-seed-workspace-file",
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
