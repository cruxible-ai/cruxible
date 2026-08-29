"""Frozen canonical and two-root semantic-coordinate laws."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.approval_policy import ApprovalPolicyV1
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    BootstrapRoot,
    CanonicalEncodingError,
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    manifest_root,
)
from cruxible_client.contracts.types import GenerationDescriptor, PrincipalRecord, StorageLayout
from cruxible_core.playbill.bootstrap import (
    bootstrap_changeset_digest,
    bootstrap_root,
    generation_root,
    genesis_semantic_root,
    genesis_tree,
)


def test_bootstrap_root_and_change_set_have_frozen_golden_preimages() -> None:
    root = bootstrap_root(instance_id="inst_test", daemon_public_key="00" * 32)
    assert root == BootstrapRoot("dc86396a0480a0d5be230e5176220e3846f908a534fb3e3f2e0c940995324637")
    assert bootstrap_changeset_digest(root).value == (
        "e56e395804efe87678b46a65ceb937313e50ce09c8d4359c63618c670be7986b"
    )


def test_semantic_genesis_has_a_frozen_end_to_end_golden() -> None:
    golden_path = Path(__file__).parents[1] / "goldens" / "playbill" / "semantic-genesis-v1.json"
    golden = json.loads(golden_path.read_bytes())
    assert golden["format"] == "playbill-semantic-genesis-golden-v1"

    principals = tuple(
        PrincipalRecord.model_validate(record) for record in golden["input"]["principals"]
    )
    approval_policy = ApprovalPolicyV1.model_validate(golden["input"]["approval_policy"])
    tree = genesis_tree(principals, approval_policy=approval_policy)
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
    assert manifest_root(tree).tagged == expected["manifest_root"]
    assert changeset.tagged == expected["changeset_digest"]
    assert semantic.tagged == expected["semantic_root"]


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
        "procedures/review.yaml": b"version: 1\n",
        "principals/owner.yaml": b"owner\n",
    }
    right = dict(reversed(tuple(left.items())))
    assert manifest_root(left) == manifest_root(right)

    changed = dict(left)
    changed["principals/owner.yaml"] = b"owner changed\n"
    assert manifest_root(left) != manifest_root(changed)


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
