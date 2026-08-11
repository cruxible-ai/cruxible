"""Frozen canonical and two-root semantic-coordinate laws."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.bootstrap import (
    bootstrap_changeset_digest,
    bootstrap_root,
    generation_root,
)
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    BootstrapRoot,
    CanonicalEncodingError,
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    manifest_root,
)
from cruxible_core.playbill.types import GenerationDescriptor, StorageLayout


def test_bootstrap_root_and_change_set_have_frozen_golden_preimages() -> None:
    root = bootstrap_root(instance_id="inst_test", daemon_public_key="00" * 32)
    assert root == BootstrapRoot("dc86396a0480a0d5be230e5176220e3846f908a534fb3e3f2e0c940995324637")
    assert bootstrap_changeset_digest(root).value == (
        "e56e395804efe87678b46a65ceb937313e50ce09c8d4359c63618c670be7986b"
    )


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
