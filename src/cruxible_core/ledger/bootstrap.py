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
    semantic_projection,
    typed_digest,
)
from cruxible_client.contracts.errors import BootstrapError
from cruxible_client.contracts.merkle import merkle_manifest_root
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
from cruxible_core.ledger.genesis_seeds import (
    GenesisSeedSet,
    closest_genesis_seed_set,
    current_genesis_seed_set,
    held_seed_table,
    known_genesis_seed_set,
)
from cruxible_core.ledger.git import GitLedger
from cruxible_core.ledger.semantic_root import compute_semantic_root


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
    #: The history entry this instance's genesis seeds equal (``ledger/genesis_seeds.py``).
    seed_set: GenesisSeedSet


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
    tree: Mapping[str, bytes],
    *,
    parent: BootstrapRoot,
) -> tuple[ChangeSetDigest, SemanticRoot]:
    """Name generation zero with the one semantic-root derivation.

    Its manifest root is the Merkle root of the tree's semantic projection,
    exactly as every later generation's is; it has no approvals; and its parent
    is the bootstrap anchor, which enters typed as `bootstrap` rather than as a
    semantic root.
    """

    changeset = bootstrap_changeset_digest(parent)
    root = compute_semantic_root(
        manifest_root=merkle_manifest_root(semantic_projection(tree)).tagged,
        changeset_digest=changeset.tagged,
        approval_digests=(),
        parent=parent,
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


#: The genesis seeds creation writes: each ledger-tree path and the SHA-256 of
#: its exact checked-in bytes, which live under ``seed_artifacts/genesis/<path>``.
#: They are ordinary governed artifacts from the first generation on. This is the
#: newest entry of the append-only history in ``ledger/genesis_seeds.py``; open
#: accepts any entry of that history, so an instance keeps the set it was created
#: with. Definitions that may change between releases are not seeds: the base kit
#: carries them and creation installs it as the first change after genesis
#: (``cruxible_core.kits``).
GENESIS_SEEDS: tuple[tuple[str, str], ...] = tuple(
    (seed.path, seed.sha256) for seed in current_genesis_seed_set().seeds
)


def genesis_seed_files() -> dict[str, bytes]:
    """Read the newest set's checked-in seed bytes, refusing any that drifted from it."""

    root = files("cruxible_core.governance.seed_artifacts").joinpath("genesis")
    loaded: dict[str, bytes] = {}
    for seed in current_genesis_seed_set().seeds:
        content = root.joinpath(*seed.path.split("/")).read_bytes()
        if hashlib.sha256(content).hexdigest() != seed.sha256:
            raise BootstrapError(f"genesis seed {seed.path} differs from its digest")
        loaded[seed.path] = content
    return loaded


def match_genesis_seed_set(seeds: Mapping[str, bytes]) -> GenesisSeedSet:
    """The history entry an instance's genesis seeds equal, or a refusal naming the gap.

    One SHA-256 per seed, then one table comparison per known set: open only.
    """

    held = held_seed_table(seeds)
    known = known_genesis_seed_set(held)
    if known is not None:
        return known
    closest = closest_genesis_seed_set(held)
    raise BootstrapError(
        "genesis seeded artifacts match no known genesis seed set; the closest is set "
        f"{closest.seed_set.ordinal} ({closest.seed_set.name}), which differs at "
        f"{closest.first_path} ({closest.first_reason})"
        + (
            f" and {closest.differing_paths - 1} more path(s)"
            if closest.differing_paths > 1
            else ""
        )
        + ". The instance was created by a newer release or an unreleased build: open it "
        "with that build, or move it aside and re-import it into a fresh instance"
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
    seed_set = match_genesis_seed_set(seeds)
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
        seed_set=seed_set,
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
    "match_genesis_seed_set",
    "prepare_genesis",
    "render_principal",
    "seeded_procedure_runtime_policy",
    "seeded_triggers",
    "verify_genesis",
]
