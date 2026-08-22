"""Shared CLI/MCP orchestration for planning and applying Playbill seed bundles."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cruxible_client import contracts
from cruxible_core.playbill import seed


class SeedClient(Protocol):
    """The existing served operations composed by one seed-group application."""

    def store_playbill_body(
        self, instance_id: str, content: bytes
    ) -> contracts.PlaybillCasObjectResult: ...

    def compile_playbill_authoring_input(
        self,
        instance_id: str,
        *,
        input: Mapping[str, Any],
        intent_id: str | None,
    ) -> contracts.PlaybillAuthoringPreflightResult: ...

    def submit_playbill_authoring_intent(
        self, instance_id: str, intent_id: str
    ) -> contracts.PlaybillAuthoringSubmitResult: ...

    def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI: ...

    def list_playbill_proposals(
        self,
        instance_id: str,
        *,
        status: Literal["open", "settled"] | None = None,
    ) -> contracts.PlaybillProposalList: ...

    def propose_playbill_claims(
        self,
        instance_id: str,
        *,
        authorings: list[dict[str, Any]],
        proposal_name: str,
    ) -> contracts.PlaybillClaimBatchProposal: ...

    def propose_playbill_claim_type(
        self,
        instance_id: str,
        *,
        claim_type: Mapping[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection: ...

    def propose_playbill_subject(
        self,
        instance_id: str,
        *,
        shell: Mapping[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection: ...

    def propose_playbill_document(
        self,
        instance_id: str,
        *,
        shell: Mapping[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection: ...

    def propose_playbill_query_definition(
        self,
        instance_id: str,
        *,
        query: Mapping[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection: ...


class _StrictSeedClientModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedPlanResultV1(_StrictSeedClientModel):
    tag: Literal["playbill-seed-plan-result-v1"] = "playbill-seed-plan-result-v1"
    plan: seed.SeedPlanV1
    plan_digest: str
    rendered: tuple[str, ...]


class SeedApplicationResultV1(_StrictSeedClientModel):
    tag: Literal["playbill-seed-application-v1"] = "playbill-seed-application-v1"
    proposal_name: str
    plan_digest: str
    operation_digest: str
    group_id: str
    operation: str
    entry_paths: tuple[str, ...]
    proposal_id: str
    target_ref: str
    next_group_id: str | None
    result: dict[str, Any]


def read_seed_bundle_files(root: Path) -> dict[str, bytes]:
    """Read one bundle without following symlinks or escaping its root."""

    try:
        bundle_root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise seed.SeedBundleError(f"Seed bundle directory is unavailable: {root}") from exc
    if not bundle_root.is_dir():
        raise seed.SeedBundleError(f"Not a seed bundle directory: {root}")
    files: dict[str, bytes] = {}
    for path in sorted(bundle_root.rglob("*")):
        if path.is_symlink():
            raise seed.SeedBundleError(f"Seed bundles may not contain symlinks: {path}")
        if path.is_file():
            files[path.relative_to(bundle_root).as_posix()] = path.read_bytes()
    if not files:
        raise seed.SeedBundleError(f"The seed bundle at {root} is empty")
    return files


def plan_seed_directory(root: Path, *, proposal_name: str) -> SeedPlanResultV1:
    files = read_seed_bundle_files(root)
    plan = seed.plan_seed_bundle(files, proposal_name=proposal_name)
    if not plan.groups:
        raise seed.SeedBundleError(f"The seed bundle at {root} declares nothing to propose")
    return SeedPlanResultV1(
        plan=plan,
        plan_digest=seed.seed_plan_digest(plan).tagged,
        rendered=seed.render_seed_plan(plan),
    )


def _admission_value(submitted: Mapping[str, Any], field: str) -> str:
    node: Any = submitted
    for _ in range(4):
        if not isinstance(node, dict):
            break
        admission = node.get("admission")
        if isinstance(admission, dict) and field in admission:
            return str(admission[field])
        status = node.get("status")
        if isinstance(status, dict) and field in status and status[field] is not None:
            return str(status[field])
        node = node.get("proposal")
    raise seed.SeedBundleError(f"The propose operation returned no admission {field!r}")


def _submit_seed_group(
    client: SeedClient,
    instance_id: str,
    *,
    files: Mapping[str, bytes],
    plan: seed.SeedPlanV1,
    group: seed.SeedProposalGroupV1,
) -> tuple[dict[str, Any], str]:
    proposal_name = seed.seed_group_proposal_name(plan, group)
    payloads = [json.loads(files[path].decode("utf-8")) for path in group.entry_paths]
    if group.operation == "playbill_authoring_submit":
        for path in plan.body_paths:
            client.store_playbill_body(instance_id, files[path])
        preflight = client.compile_playbill_authoring_input(
            instance_id,
            input=payloads[0],
            intent_id=None,
        )
        if preflight.verdict != "passed":
            raise seed.SeedBundleError(
                "Authoring seed preflight refused: "
                + json.dumps(preflight.frontier, ensure_ascii=False, sort_keys=True)
            )
        intent_id = preflight.certificate.get("intent_id")
        target_ref = preflight.certificate.get("proposal_ref")
        if not isinstance(intent_id, str) or not isinstance(target_ref, str):
            raise seed.SeedBundleError(
                "Authoring seed preflight omitted its intent or proposal identity"
            )
        submitted = client.submit_playbill_authoring_intent(
            instance_id,
            intent_id,
        ).model_dump(mode="json")
        if _admission_value(submitted, "proposal_id") == "":  # pragma: no cover
            raise seed.SeedBundleError("Authoring seed submit omitted its proposal ID")
        return submitted, target_ref

    whoami = client.playbill_whoami(instance_id)
    target_ref = f"refs/proposals/{whoami.actor_id}/{proposal_name}"
    open_proposals = client.list_playbill_proposals(instance_id, status="open")
    existing = next(
        (entry for entry in open_proposals.entries if entry.target_ref == target_ref),
        None,
    )
    if existing is not None:
        raise seed.SeedBundleError(
            f"Seed group {group.group_id!r} already has open proposal "
            f"{existing.proposal_id} at {target_ref}"
        )

    for path in plan.body_paths:
        client.store_playbill_body(instance_id, files[path])

    if group.kind == "claim":
        submitted = client.propose_playbill_claims(
            instance_id,
            authorings=payloads,
            proposal_name=proposal_name,
        ).model_dump(mode="json")
    else:
        single = payloads[0]
        if group.kind == "claim_type":
            submitted = client.propose_playbill_claim_type(
                instance_id,
                claim_type=single,
                proposal_name=proposal_name,
            ).model_dump(mode="json")
        elif group.kind == "subject":
            submitted = client.propose_playbill_subject(
                instance_id,
                shell=single,
                proposal_name=proposal_name,
            ).model_dump(mode="json")
        elif group.kind == "document":
            submitted = client.propose_playbill_document(
                instance_id,
                shell=single,
                proposal_name=proposal_name,
            ).model_dump(mode="json")
        else:
            submitted = client.propose_playbill_query_definition(
                instance_id,
                query=single,
                proposal_name=proposal_name,
            ).model_dump(mode="json")
    if _admission_value(submitted, "target_ref") != target_ref:
        raise seed.SeedBundleError("The seed proposal admission returned an unexpected target ref")
    return submitted, target_ref


def apply_seed_directory_group(
    client: SeedClient,
    instance_id: str,
    *,
    root: Path,
    proposal_name: str,
    group_id: str | None = None,
) -> SeedApplicationResultV1:
    files = read_seed_bundle_files(root)
    plan = seed.plan_seed_bundle(files, proposal_name=proposal_name)
    if not plan.groups:
        raise seed.SeedBundleError(f"The seed bundle at {root} declares nothing to propose")
    group = plan.group(group_id) if group_id is not None else plan.groups[0]
    submitted, target_ref = _submit_seed_group(
        client,
        instance_id,
        files=files,
        plan=plan,
        group=group,
    )
    return SeedApplicationResultV1(
        proposal_name=plan.proposal_name,
        plan_digest=seed.seed_plan_digest(plan).tagged,
        operation_digest=seed.seed_group_operation_digest(plan, group).tagged,
        group_id=group.group_id,
        operation=group.operation,
        entry_paths=group.entry_paths,
        proposal_id=_admission_value(submitted, "proposal_id"),
        target_ref=target_ref,
        next_group_id=plan.next_group_id(group.group_id),
        result=submitted,
    )


__all__ = [
    "SeedApplicationResultV1",
    "SeedClient",
    "SeedPlanResultV1",
    "apply_seed_directory_group",
    "plan_seed_directory",
    "read_seed_bundle_files",
]
