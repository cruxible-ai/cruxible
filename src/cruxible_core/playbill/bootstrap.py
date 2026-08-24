"""Exact Playbill generation-zero preparation and replay verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from cruxible_client.contracts.canonical import (
    BootstrapRoot,
    ChangeSetDigest,
    GenerationRoot,
    SemanticRoot,
    manifest_root,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillBootstrapError
from cruxible_client.contracts.principal_rendering import render_principal
from cruxible_client.contracts.types import (
    GenerationDescriptor,
    PlaybillTrustRoot,
    PrincipalRecord,
)
from cruxible_core.playbill.git import GitLedger


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


def bootstrap_root(*, instance_id: str, daemon_public_key: str) -> BootstrapRoot:
    """Compute exact `P_0` from instance identity and raw daemon public key."""

    try:
        key_bytes = bytes.fromhex(daemon_public_key)
    except ValueError as exc:
        raise PlaybillBootstrapError("daemon public key must be lowercase hex") from exc
    if len(key_bytes) != 32 or key_bytes.hex() != daemon_public_key:
        raise PlaybillBootstrapError("daemon public key must contain 32 lowercase-hex bytes")
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


def genesis_tree(principals: Sequence[PrincipalRecord]) -> dict[str, bytes]:
    ordered = sorted(principals, key=lambda record: record.principal_id)
    if [record.principal_id for record in ordered] != sorted(
        {record.principal_id for record in ordered}
    ):
        raise PlaybillBootstrapError("genesis principals must be unique")
    return {
        f"principals/{record.principal_id}.yaml": render_principal(record) for record in ordered
    }


def verify_genesis(
    ledger: GitLedger,
    oid: str,
    *,
    trust_root: PlaybillTrustRoot,
) -> VerifiedGenesis:
    """Replay generation zero from out-of-band instance, key, and principals."""

    if ledger.parent_of(oid) is not None:
        raise PlaybillBootstrapError("genesis commit unexpectedly has a Git parent")
    if ledger.allowed_signer_public_key_hex("daemon") != trust_root.daemon_public_key:
        raise PlaybillBootstrapError("allowed daemon signer differs from bootstrap key")
    if not ledger.verify_commit(oid):
        raise PlaybillBootstrapError("genesis is not signed by the bootstrap daemon key")

    tree = ledger.read_tree(oid)
    expected_tree = genesis_tree(trust_root.principals)
    if set(tree) != set(expected_tree):
        raise PlaybillBootstrapError("genesis principal registry paths differ from trust root")

    parsed: list[PrincipalRecord] = []
    for path in sorted(expected_tree):
        content = tree[path]
        try:
            payload = json.loads(content)
            record = PrincipalRecord.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            raise PlaybillBootstrapError(f"invalid canonical genesis principal: {path}") from exc
        if render_principal(record) != content:
            raise PlaybillBootstrapError(f"genesis principal is not canonical: {path}")
        if content != expected_tree[path]:
            raise PlaybillBootstrapError(f"genesis principal differs from trust root: {path}")
        parsed.append(record)

    daemon = next(record for record in parsed if record.principal_id == "daemon")
    if daemon.public_key != trust_root.daemon_public_key:
        raise PlaybillBootstrapError("committed daemon principal differs from bootstrap key")

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
    )


def prepare_genesis(
    ledger: GitLedger,
    *,
    trust_root: PlaybillTrustRoot,
    timestamp: str,
) -> VerifiedGenesis:
    """Create, verify, and install the one no-parent genesis commit."""

    tree = genesis_tree(trust_root.principals)
    oid = ledger.create_signed_genesis(tree, timestamp=timestamp)
    verified = verify_genesis(ledger, oid, trust_root=trust_root)
    ledger.set_main_genesis(oid)
    if ledger.read_main() != oid:
        raise PlaybillBootstrapError("main did not settle on the verified genesis commit")
    return verified


__all__ = [
    "VerifiedGenesis",
    "bootstrap_changeset_digest",
    "bootstrap_root",
    "generation_root",
    "genesis_semantic_root",
    "genesis_tree",
    "prepare_genesis",
    "render_principal",
    "verify_genesis",
]
