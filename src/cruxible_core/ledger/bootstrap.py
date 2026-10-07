"""Exact Cruxible generation-zero preparation and replay verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
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
    P2_B0_ARTIFACT_CODEC,
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
    procedure_runtime_policy: ProcedureRuntimePolicy | None
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
    procedure_runtime_policy: ProcedureRuntimePolicy | None = None,
    seeds: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    ordered = sorted(principals, key=lambda record: record.principal_id)
    if [record.principal_id for record in ordered] != sorted(
        {record.principal_id for record in ordered}
    ):
        raise BootstrapError("genesis principals must be unique")
    tree = {
        APPROVAL_POLICY_PATH: render_approval_policy(approval_policy),
        **{
            f"principals/{record.principal_id}.json": render_principal(record) for record in ordered
        },
    }
    if procedure_runtime_policy is not None:
        tree[PROCEDURE_RUNTIME_POLICY_PATH] = render_procedure_runtime_policy(
            procedure_runtime_policy
        )
    for path, content in (seeds or {}).items():
        if not _is_genesis_seed_path(path):
            raise BootstrapError(f"genesis seed collides with a bootstrap path: {path}")
        tree[path] = content
    return tree


@dataclass(frozen=True)
class GenesisSeedSet:
    """One frozen set of artifacts some Cruxible build seeded at generation zero.

    ``files`` maps each ledger-tree path to the SHA-256 of its exact bytes;
    the bytes live under ``seed_artifacts/genesis/<set_id>/<path>``.
    ``artifact_kinds`` are the kinds an instance's compiler must admit for
    the set to be seeded.
    """

    set_id: str
    files: tuple[tuple[str, str], ...]
    artifact_kinds: frozenset[str]

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(path for path, _digest in self.files)


#: Every genesis seed set any Cruxible build has written, oldest first.
#:
#: Append-only: an instance keeps its generation zero forever, so verification
#: accepts a genesis whose seeded artifacts equal one of these sets exactly.
#: Never edit or remove an entry or its checked-in bytes; a change to what new
#: instances start with is a new entry at the end.
GENESIS_SEED_SETS: tuple[GenesisSeedSet, ...] = (
    # Instances from before seeded Triggers, and compilers that admit none.
    GenesisSeedSet(set_id="empty", files=(), artifact_kinds=frozenset()),
    # 2026-10-02 (cf3920625): sweep, floor refresh and anchor retry.
    GenesisSeedSet(
        set_id="triggers-3",
        files=(
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
        ),
        artifact_kinds=frozenset({"trigger"}),
    ),
    # 2026-10-07 (11225019f): adds curation detection.
    GenesisSeedSet(
        set_id="triggers-4",
        files=(
            (
                "triggers/curation-detect.json",
                "3d4739fe1f7e1bc2cc2d9769c8e15d87bf3f73820750894e269299b878c753ae",
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
        ),
        artifact_kinds=frozenset({"trigger"}),
    ),
    # workspace.file as a core built-in: its compiler-owned ProviderInterface
    # registration and the cruxible-builtin Provider
    # (governance/seed_artifacts/workspace_file.py).
    GenesisSeedSet(
        set_id="triggers-4-workspace-file",
        files=(
            (
                "provider-interfaces/workspace.file.json",
                "f3afe32fd84a09b991edaba2c02110f05b5854bccf6db16c4c7379ebf1d205fe",
            ),
            (
                "providers/cruxible-builtin.json",
                "0735895428d89309e507ba0ce4f07292f17c4986a1f8b5a6a35dc548574d6236",
            ),
            (
                "triggers/curation-detect.json",
                "3d4739fe1f7e1bc2cc2d9769c8e15d87bf3f73820750894e269299b878c753ae",
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
        ),
        artifact_kinds=frozenset({"trigger", "provider-interface", "provider"}),
    ),
)


def genesis_seed_set(set_id: str) -> GenesisSeedSet:
    for seed_set in GENESIS_SEED_SETS:
        if seed_set.set_id == set_id:
            return seed_set
    raise KeyError(set_id)


def current_genesis_seed_set(admitted_kinds: Iterable[str]) -> GenesisSeedSet:
    """The newest seed set whose artifact kinds the instance's compiler admits."""

    admitted = frozenset(admitted_kinds)
    return next(
        seed_set for seed_set in reversed(GENESIS_SEED_SETS) if seed_set.artifact_kinds <= admitted
    )


def genesis_seed_files(seed_set: GenesisSeedSet) -> dict[str, bytes]:
    """Read one set's checked-in bytes, refusing any that drifted from the table."""

    root = files("cruxible_core.governance.seed_artifacts").joinpath("genesis", seed_set.set_id)
    loaded: dict[str, bytes] = {}
    for path, digest in seed_set.files:
        content = root.joinpath(*path.split("/")).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise BootstrapError(f"genesis seed {seed_set.set_id}/{path} differs from its digest")
        loaded[path] = content
    return loaded


def matching_genesis_seed_set(seeds: Mapping[str, bytes]) -> GenesisSeedSet | None:
    """The historical set these seeded artifacts equal exactly, if any."""

    for seed_set in GENESIS_SEED_SETS:
        if set(seeds) == seed_set.paths and all(
            hashlib.sha256(seeds[path]).hexdigest() == digest for path, digest in seed_set.files
        ):
            return seed_set
    return None


def seeded_triggers() -> tuple[Trigger, ...]:
    """The Triggers a new instance starts with (those of the newest seed set).

    They are ordinary governed Triggers from the first generation on: an
    instance changes or retires them through proposals like any other.
    """

    return tuple(
        parse_trigger(content, path=path)
        for path, content in genesis_seed_files(GENESIS_SEED_SETS[-1]).items()
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
        .joinpath("procedure-runtime-policy.yaml")
        .read_bytes(),
        path="governance/procedure-runtime-policy.yaml",
        codec=P2_B0_ARTIFACT_CODEC,
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
    runtime_policy: ProcedureRuntimePolicy | None = None
    runtime_policy_content = tree.get(PROCEDURE_RUNTIME_POLICY_PATH)
    if runtime_policy_content is not None:
        try:
            runtime_policy = parse_procedure_runtime_policy(
                runtime_policy_content,
                path=PROCEDURE_RUNTIME_POLICY_PATH,
            )
        except ProcedureRuntimePolicyFormatError as exc:
            raise BootstrapError("genesis Procedure runtime policy is invalid") from exc
    seeds = {path: content for path, content in tree.items() if _is_genesis_seed_path(path)}
    seed_set = matching_genesis_seed_set(seeds)
    if seed_set is None:
        raise BootstrapError("genesis seeded artifacts match no historical genesis seed set")
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
    seed_set: GenesisSeedSet | None = None,
    timestamp: str,
) -> VerifiedGenesis:
    """Create, verify, and install the one no-parent genesis commit.

    ``seed_set`` is the historical set the instance starts with, chosen by
    the artifact kinds its compiler admits (none when omitted).
    """

    tree = genesis_tree(
        trust_root.principals,
        approval_policy=approval_policy,
        procedure_runtime_policy=(
            seeded_procedure_runtime_policy()
            if procedure_runtime_policy is None
            else procedure_runtime_policy
        ),
        seeds=genesis_seed_files(seed_set) if seed_set is not None else None,
    )
    oid = ledger.create_signed_genesis(tree, timestamp=timestamp)
    verified = verify_genesis(ledger, oid, trust_root=trust_root)
    ledger.set_main_genesis(oid)
    if ledger.read_main() != oid:
        raise BootstrapError("main did not settle on the verified genesis commit")
    return verified


__all__ = [
    "GENESIS_SEED_SETS",
    "GenesisSeedSet",
    "VerifiedGenesis",
    "bootstrap_changeset_digest",
    "bootstrap_root",
    "current_genesis_seed_set",
    "generation_root",
    "genesis_seed_files",
    "genesis_seed_set",
    "genesis_semantic_root",
    "genesis_tree",
    "matching_genesis_seed_set",
    "prepare_genesis",
    "render_principal",
    "seeded_procedure_runtime_policy",
    "seeded_triggers",
    "verify_genesis",
]
