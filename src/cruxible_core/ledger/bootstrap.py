"""Exact Cruxible generation-zero preparation and replay verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files

from pydantic import ValidationError

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    ApprovalPolicy,
    ApprovalPolicyFormatError,
    parse_approval_policy,
    render_approval_policy,
)
from cruxible_client.contracts.canonical import (
    BootstrapRoot,
    ChangeSetDigest,
    GenerationRoot,
    SemanticRoot,
    manifest_root,
    typed_digest,
)
from cruxible_client.contracts.errors import BootstrapError
from cruxible_client.contracts.principal_rendering import render_principal
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_PATH,
    ProcedureRuntimePolicy,
    ProcedureRuntimePolicyFormatError,
    parse_procedure_runtime_policy,
    render_procedure_runtime_policy,
)
from cruxible_client.contracts.triggers import Trigger, parse_trigger
from cruxible_client.contracts.types import (
    GenerationDescriptor,
    PrincipalRecord,
    TrustRoot,
)
from cruxible_core.ledger.git import GitLedger


@dataclass(frozen=True)
class VerifiedGenesis:
    """The complete reproducible result of generation-zero verification."""

    oid: str
    tree: dict[str, bytes]
    bootstrap_root: BootstrapRoot
    changeset_digest: ChangeSetDigest
    semantic_root: SemanticRoot
    descriptor: GenerationDescriptor
    generation_root: GenerationRoot
    principals: tuple[PrincipalRecord, ...]
    approval_policy: ApprovalPolicy
    procedure_runtime_policy: ProcedureRuntimePolicy


def bootstrap_root(*, instance_id: str, daemon_public_key: str) -> BootstrapRoot:
    """Compute exact `P_0` from instance identity and raw daemon public key."""

    try:
        key_bytes = bytes.fromhex(daemon_public_key)
    except ValueError as exc:
        raise BootstrapError("daemon public key must be lowercase hex") from exc
    if len(key_bytes) != 32 or key_bytes.hex() != daemon_public_key:
        raise BootstrapError("daemon public key must contain 32 lowercase-hex bytes")
    return typed_digest(
        BootstrapRoot,
        "playbill-genesis-v1",
        {
            "instance_id": instance_id,
            "bootstrap_key_digest": hashlib.sha256(key_bytes).hexdigest(),
        },
    )


def bootstrap_changeset_digest(parent: BootstrapRoot) -> ChangeSetDigest:
    return typed_digest(
        ChangeSetDigest,
        "playbill-changeset-v1",
        {"genesis_parent_semantic_root": parent.value},
    )


def genesis_semantic_root(
    tree: dict[str, bytes],
    *,
    parent: BootstrapRoot,
) -> tuple[ChangeSetDigest, SemanticRoot]:
    changeset = bootstrap_changeset_digest(parent)
    root = typed_digest(
        SemanticRoot,
        "playbill-sroot-v1",
        {
            "manifest_root": manifest_root(tree).value,
            "changeset_digest": changeset.value,
            "approval_digests": [],
            "parent_semantic_root": parent.value,
        },
    )
    return changeset, root


def generation_root(descriptor: GenerationDescriptor) -> GenerationRoot:
    return typed_digest(
        GenerationRoot,
        "playbill-gen-v1",
        {
            "semantic_root": descriptor.semantic_root,
            "git_oid": descriptor.git_oid,
            "parent_generation_root": descriptor.parent_generation_root,
        },
    )


def genesis_tree(
    principals: Sequence[PrincipalRecord],
    *,
    approval_policy: ApprovalPolicy,
    procedure_runtime_policy: ProcedureRuntimePolicy,
    seeds: Mapping[str, bytes],
) -> dict[str, bytes]:
    ordered = sorted(principals, key=lambda record: record.principal_id)
    if [record.principal_id for record in ordered] != sorted(
        {record.principal_id for record in ordered}
    ):
        raise BootstrapError("genesis principals must be unique")
    tree = {
        APPROVAL_POLICY_PATH: render_approval_policy(approval_policy),
        PROCEDURE_RUNTIME_POLICY_PATH: render_procedure_runtime_policy(procedure_runtime_policy),
        **{
            f"principals/{record.principal_id}.json": render_principal(record) for record in ordered
        },
    }
    for path, content in seeds.items():
        if not _is_genesis_seed_path(path):
            raise BootstrapError(f"genesis seed collides with a bootstrap path: {path}")
        tree[path] = content
    return tree


#: The artifacts every new instance starts with at generation zero: each
#: ledger-tree path and the SHA-256 of its exact checked-in bytes, which live
#: under ``seed_artifacts/genesis/<path>``. They are ordinary governed
#: artifacts from the first generation on. Verification admits exactly this
#: set: a genesis seeded with anything else refuses (an instance from an earlier
#: build is moved aside and re-imported fresh, never converted).
GENESIS_SEEDS: tuple[tuple[str, str], ...] = (
    (
        "claim-types/semantic/alias.json",
        "0d2449bf47e8375e2c43395099234b09c28391e4ac576ab0bd688e858dd40999",
    ),
    (
        "claim-types/semantic/distinct_from.json",
        "a66106eaf0827455c61414ccaaaeb60300bf40499a8d59f994fe2d275d70fd95",
    ),
    (
        "contracts/workspace.file.content/v1.json",
        "a0a8908cbc65d9b5320a3bc34ebabd44356a7580e645509784819f60595ff5be",
    ),
    (
        "contracts/workspace.file.refusal.invalid_parameter/v1.json",
        "473440364b8c5c641aa7cd765ad4cb0b897e1fbafc18aad545b59e80b66021a8",
    ),
    (
        "contracts/workspace.file.refusal.mismatched_lengths/v1.json",
        "73646057965632d5fde5208a7d814fb3459057cbdb98488466eab81ea7f12868",
    ),
    (
        "contracts/workspace.file.refusal.provider_declined/v1.json",
        "989bc9541520590f0a9425488f477e97e0e515472d3003985667fa5bf54c29c5",
    ),
    (
        "contracts/workspace.file.request/v1.json",
        "c9ed78bc1265f0169e6e05c8f1a5cdec8f6dcc0cbff91de3d74f1e4bd43cd70c",
    ),
    (
        "contracts/workspace.file.result/v1.json",
        "e3966d5a88323a68de2da9e70d2513ab4db9750e239a82b6f4efffdaf1944b5c",
    ),
    (
        "contracts/workspace.file.source/v1.json",
        "c8508b48cb424fad39a34df454fa403cedece340df708f80c422293ccc663062",
    ),
    (
        "providers/cruxible-builtin.json",
        "0ef8007bf03ec92657a0b67f8f7da93be071d185070cb545913c147157d4c392",
    ),
    (
        "triggers/evidence-sweep.json",
        "6dc35c2d7e6b4a0d028075278e798f6f44980450a169b640e696cc1b22b5218a",
    ),
    (
        "triggers/floor-refresh.json",
        "fa726264af5e81c6dfcc6dbf4ade34b8c471b2749e4027a9bb4ff3f95169d98a",
    ),
    (
        "triggers/prediction-anchor-retry.json",
        "2e3c2183b59b6f2025fd8a6351fb6721fc5210d39626527b210302016112fa72",
    ),
)


def genesis_seed_files() -> dict[str, bytes]:
    """Read the checked-in genesis seed bytes, refusing any that drifted from the table."""

    root = files("cruxible_core.governance.seed_artifacts").joinpath("genesis")
    loaded: dict[str, bytes] = {}
    for path, digest in GENESIS_SEEDS:
        content = root.joinpath(*path.split("/")).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise BootstrapError(f"genesis seed {path} differs from its digest")
        loaded[path] = content
    return loaded


def _is_current_genesis_seeds(seeds: Mapping[str, bytes]) -> bool:
    return set(seeds) == {path for path, _digest in GENESIS_SEEDS} and all(
        hashlib.sha256(seeds[path]).hexdigest() == digest for path, digest in GENESIS_SEEDS
    )


def seeded_triggers() -> tuple[Trigger, ...]:
    """The Triggers a new instance starts with (those of the genesis seeds).

    They are ordinary governed Triggers from the first generation on: an
    instance changes or retires them through proposals like any other.
    """

    return tuple(
        parse_trigger(content, path=path)
        for path, content in genesis_seed_files().items()
        if path.startswith("triggers/")
    )


def _is_genesis_seed_path(path: str) -> bool:
    return path not in {APPROVAL_POLICY_PATH, PROCEDURE_RUNTIME_POLICY_PATH} and not (
        path.startswith("principals/")
    )


def seeded_procedure_runtime_policy() -> ProcedureRuntimePolicy:
    """Load the checked-in genesis policy artifact; no runtime cap lives in code."""

    return parse_procedure_runtime_policy(
        files("cruxible_core.governance.seed_artifacts")
        .joinpath("procedure-runtime-policy.json")
        .read_bytes(),
        path=PROCEDURE_RUNTIME_POLICY_PATH,
    )


def verify_genesis(
    ledger: GitLedger,
    oid: str,
    *,
    trust_root: TrustRoot,
) -> VerifiedGenesis:
    """Replay generation zero from out-of-band instance, key, and principals."""

    if ledger.parent_of(oid) is not None:
        raise BootstrapError("genesis commit unexpectedly has a Git parent")
    if ledger.allowed_signer_public_key_hex("daemon") != trust_root.daemon_public_key:
        raise BootstrapError("allowed daemon signer differs from bootstrap key")
    if not ledger.verify_commit(oid):
        raise BootstrapError("genesis is not signed by the bootstrap daemon key")

    tree = ledger.read_tree(oid)
    policy_content = tree.get(APPROVAL_POLICY_PATH)
    if policy_content is None:
        raise BootstrapError("genesis approval policy is missing")
    try:
        approval_policy = parse_approval_policy(policy_content, path=APPROVAL_POLICY_PATH)
    except ApprovalPolicyFormatError as exc:
        raise BootstrapError("genesis approval policy is invalid") from exc
    runtime_policy_content = tree.get(PROCEDURE_RUNTIME_POLICY_PATH)
    if runtime_policy_content is None:
        raise BootstrapError("genesis Procedure runtime policy is missing")
    try:
        runtime_policy = parse_procedure_runtime_policy(
            runtime_policy_content,
            path=PROCEDURE_RUNTIME_POLICY_PATH,
        )
    except ProcedureRuntimePolicyFormatError as exc:
        raise BootstrapError("genesis Procedure runtime policy is invalid") from exc
    seeds = {path: content for path, content in tree.items() if _is_genesis_seed_path(path)}
    if not _is_current_genesis_seeds(seeds):
        raise BootstrapError(
            "genesis seeded artifacts differ from this release's genesis seeds; an instance "
            "from an earlier build is moved aside and re-imported into a fresh instance"
        )
    expected_tree = genesis_tree(
        trust_root.principals,
        approval_policy=approval_policy,
        procedure_runtime_policy=runtime_policy,
        seeds=seeds,
    )
    if set(tree) != set(expected_tree):
        raise BootstrapError("genesis principal registry paths differ from trust root")

    parsed: list[PrincipalRecord] = []
    for path in sorted(expected_tree):
        if path in {APPROVAL_POLICY_PATH, PROCEDURE_RUNTIME_POLICY_PATH}:
            if tree[path] != expected_tree[path]:  # pragma: no cover - parser already proves this
                raise BootstrapError("genesis approval policy is not canonical")
            continue
        if path in seeds:
            continue
        content = tree[path]
        try:
            payload = json.loads(content)
            record = PrincipalRecord.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            raise BootstrapError(f"invalid canonical genesis principal: {path}") from exc
        if render_principal(record) != content:
            raise BootstrapError(f"genesis principal is not canonical: {path}")
        if content != expected_tree[path]:
            raise BootstrapError(f"genesis principal differs from trust root: {path}")
        parsed.append(record)

    daemon = next(record for record in parsed if record.principal_id == "daemon")
    if daemon.public_key != trust_root.daemon_public_key:
        raise BootstrapError("committed daemon principal differs from bootstrap key")

    parent = bootstrap_root(
        instance_id=trust_root.instance_id,
        daemon_public_key=trust_root.daemon_public_key,
    )
    changeset, semantic = genesis_semantic_root(tree, parent=parent)
    descriptor = GenerationDescriptor(
        semantic_root=semantic.value,
        git_oid=oid,
        parent_generation_root=parent.value,
    )
    return VerifiedGenesis(
        oid=oid,
        tree=tree,
        bootstrap_root=parent,
        changeset_digest=changeset,
        semantic_root=semantic,
        descriptor=descriptor,
        generation_root=generation_root(descriptor),
        principals=tuple(parsed),
        approval_policy=approval_policy,
        procedure_runtime_policy=runtime_policy,
    )


def prepare_genesis(
    ledger: GitLedger,
    *,
    trust_root: TrustRoot,
    approval_policy: ApprovalPolicy,
    procedure_runtime_policy: ProcedureRuntimePolicy | None = None,
    timestamp: str,
) -> VerifiedGenesis:
    """Create, verify, and install the one no-parent genesis commit with the genesis seeds."""

    tree = genesis_tree(
        trust_root.principals,
        approval_policy=approval_policy,
        procedure_runtime_policy=(
            seeded_procedure_runtime_policy()
            if procedure_runtime_policy is None
            else procedure_runtime_policy
        ),
        seeds=genesis_seed_files(),
    )
    oid = ledger.create_signed_genesis(tree, timestamp=timestamp)
    verified = verify_genesis(ledger, oid, trust_root=trust_root)
    ledger.set_main_genesis(oid)
    if ledger.read_main() != oid:
        raise BootstrapError("main did not settle on the verified genesis commit")
    return verified


__all__ = [
    "GENESIS_SEEDS",
    "VerifiedGenesis",
    "bootstrap_changeset_digest",
    "bootstrap_root",
    "generation_root",
    "genesis_seed_files",
    "genesis_semantic_root",
    "genesis_tree",
    "prepare_genesis",
    "render_principal",
    "seeded_procedure_runtime_policy",
    "seeded_triggers",
    "verify_genesis",
]
