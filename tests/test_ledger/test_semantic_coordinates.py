"""Frozen canonical and two-root semantic-coordinate laws."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.approval_policy import ApprovalPolicy
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    BootstrapRoot,
    CanonicalEncodingError,
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    semantic_projection,
)
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.merkle import merkle_manifest_root
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy
from cruxible_client.contracts.types import GenerationDescriptor, PrincipalRecord, StorageLayout
from cruxible_core.ledger.bootstrap import (
    bootstrap_changeset_digest,
    bootstrap_root,
    generation_root,
    genesis_semantic_root,
    genesis_tree,
)
from cruxible_core.ledger.semantic_root import compute_semantic_root, parent_derivation


def test_bootstrap_root_and_change_set_have_frozen_golden_preimages() -> None:
    root = bootstrap_root(instance_id="inst_test", daemon_public_key="00" * 32)
    assert root == BootstrapRoot("dc86396a0480a0d5be230e5176220e3846f908a534fb3e3f2e0c940995324637")
    assert bootstrap_changeset_digest(root).value == (
        "e56e395804efe87678b46a65ceb937313e50ce09c8d4359c63618c670be7986b"
    )


def test_semantic_genesis_has_a_frozen_end_to_end_golden() -> None:
    """Generation zero is named by sroot-v2 over its Merkle manifest, parented by bootstrap."""

    golden_path = Path(__file__).parents[1] / "goldens" / "playbill" / "semantic-genesis-v2.json"
    golden = json.loads(golden_path.read_bytes())
    assert golden["format"] == "playbill-semantic-genesis-golden-v2"

    principals = tuple(
        PrincipalRecord.model_validate(record) for record in golden["input"]["principals"]
    )
    approval_policy = ApprovalPolicy.model_validate(golden["input"]["approval_policy"])
    runtime_policy = ProcedureRuntimePolicy.model_validate(
        golden["input"]["procedure_runtime_policy"]
    )
    tree = genesis_tree(
        principals,
        approval_policy=approval_policy,
        procedure_runtime_policy=runtime_policy,
        seeds={},
    )
    parent = bootstrap_root(
        instance_id=golden["input"]["instance_id"],
        daemon_public_key=golden["input"]["daemon_public_key"],
    )
    changeset, semantic = genesis_semantic_root(tree, parent=parent)

    expected = golden["expected"]
    expected_tree = {
        path: content.encode("utf-8") for path, content in expected["canonical_tree"].items()
    }
    assert tree == expected_tree
    assert parent.tagged == expected["bootstrap_root"]
    manifest = merkle_manifest_root(semantic_projection(tree))
    assert manifest.tagged == expected["manifest_root"]
    assert manifest.tagged.startswith("merkle-sha256:")
    assert changeset.tagged == expected["changeset_digest"]
    assert semantic.tagged == expected["semantic_root"]

    # The exact preimage: the one derivation's six fields, no approvals, and the
    # bootstrap anchor entering as a typed `bootstrap` parent.
    preimage = expected["semantic_root_preimage"].encode("utf-8")
    assert preimage == canonical_bytes(
        {
            "tag": "playbill-sroot-v2",
            "manifest_root": manifest.tagged,
            "changeset_digest": changeset.tagged,
            "approval_digests": [],
            "parent_semantic_root": parent.tagged,
            "parent_derivation": "bootstrap",
        }
    )
    assert semantic == SemanticRoot(hashlib.sha256(preimage).hexdigest())
    assert semantic == compute_semantic_root(
        manifest_root=manifest.tagged,
        changeset_digest=changeset.tagged,
        approval_digests=(),
        parent=parent,
    )


def test_genesis_root_parent_kind_is_typed_and_never_a_semantic_root() -> None:
    """The same 32 bytes as a bootstrap anchor and as a semantic root name different children."""

    tree = {"principals/owner.json": b"owner\n"}
    parent = bootstrap_root(instance_id="inst_test", daemon_public_key="00" * 32)
    changeset, semantic = genesis_semantic_root(tree, parent=parent)
    assert parent_derivation(parent) == "bootstrap"
    assert parent_derivation(SemanticRoot(parent.value)) == "playbill-sroot-v2"
    claimed_as_root = compute_semantic_root(
        manifest_root=merkle_manifest_root(tree).tagged,
        changeset_digest=changeset.tagged,
        approval_digests=(),
        parent=SemanticRoot(parent.value),
    )
    assert claimed_as_root != semantic
    with pytest.raises(SettlementIntegrityError, match="bootstrap root or a semantic root"):
        compute_semantic_root(
            manifest_root=merkle_manifest_root(tree).tagged,
            changeset_digest=changeset.tagged,
            approval_digests=(),
            parent=GenerationRoot(parent.value),  # type: ignore[arg-type]
        )


def test_genesis_semantic_root_is_member_sensitive_and_ignores_derivative_namespaces() -> None:
    tree = {"principals/owner.json": b"owner\n", "governance/approval-policy.json": b"{}\n"}
    parent = bootstrap_root(instance_id="inst_test", daemon_public_key="00" * 32)
    _changeset, semantic = genesis_semantic_root(tree, parent=parent)
    altered = {**tree, "principals/owner.json": b"owner altered\n"}
    assert genesis_semantic_root(altered, parent=parent)[1] != semantic
    other_parent = bootstrap_root(instance_id="inst_other", daemon_public_key="00" * 32)
    assert genesis_semantic_root(tree, parent=other_parent)[1] != semantic
    # The root covers the semantic projection, as every later generation's does.
    with_card = {**tree, "cards/owner.txt": b"card\n"}
    assert genesis_semantic_root(with_card, parent=parent)[1] == semantic


def test_generation_root_uses_exact_three_field_preimage() -> None:
    descriptor = GenerationDescriptor(
        semantic_root="11" * 32,
        git_oid="22" * 20,
        parent_generation_root="33" * 32,
    )
    exact = canonical_bytes(
        {
            "tag": "playbill-gen-v1",
            "semantic_root": "11" * 32,
            "git_oid": "22" * 20,
            "parent_generation_root": "33" * 32,
        }
    )
    assert generation_root(descriptor) == GenerationRoot(hashlib.sha256(exact).hexdigest())
    assert set(descriptor.model_dump()) == {
        "tag",
        "semantic_root",
        "git_oid",
        "parent_generation_root",
    }

    with pytest.raises(ValidationError):
        GenerationDescriptor(
            semantic_root="11" * 32,
            git_oid="22" * 20,
            parent_generation_root=None,  # type: ignore[arg-type]
        )


def test_digest_types_are_algorithm_tagged_and_kind_distinct() -> None:
    digest = "ab" * 32
    assert ArtifactDigest(digest).tagged == f"sha256:{digest}"
    assert SemanticRoot.from_tagged(f"sha256:{digest}") == SemanticRoot(digest)
    assert ArtifactDigest(digest) != SemanticRoot(digest)

    for malformed in (digest, f"SHA256:{digest}", f"sha1:{digest}", "sha256:AB"):
        with pytest.raises(ValueError):
            Sha256Value.from_tagged(malformed)


def test_canonical_encoding_refuses_ambiguous_runtime_values() -> None:
    for value in (1.5, b"bytes", ("tuple",)):
        with pytest.raises(CanonicalEncodingError):
            canonical_bytes(value)

    with pytest.raises(CanonicalEncodingError, match="collide"):
        canonical_bytes({"e\u0301": 1, "é": 2})


def test_manifest_root_is_order_independent_and_content_sensitive() -> None:
    left = {
        "procedures/review.json": b"version: 1\n",
        "principals/owner.json": b"owner\n",
    }
    right = dict(reversed(tuple(left.items())))
    assert merkle_manifest_root(left) == merkle_manifest_root(right)

    changed = dict(left)
    changed["principals/owner.json"] = b"owner changed\n"
    assert merkle_manifest_root(left) != merkle_manifest_root(changed)


@pytest.mark.parametrize(
    "value",
    ("/absolute", "a//b", "a/./b", "a/../b", "a\\b"),
)
def test_storage_layout_refuses_noncanonical_or_escaping_paths(value: str) -> None:
    with pytest.raises(ValueError):
        StorageLayout(ledger=value)


def test_storage_layout_refuses_overlapping_authority_paths() -> None:
    with pytest.raises(ValueError, match="contain"):
        StorageLayout(ledger="managed", credentials="managed/credentials")
