"""The one semantic-root derivation that names every accepted generation.

Generation zero and every receipt-bearing generation after it are named by the
same function under the same domain, `playbill-sroot-v2`. Its preimage is
exactly, and only:

- `tag`: `"playbill-sroot-v2"`, supplied by the canonical digest domain;
- `manifest_root`: the semantic projection's Merkle manifest root, in its
  tagged `merkle-sha256:` spelling;
- `changeset_digest`: the change-set digest's tagged spelling;
- `approval_digests`: the sorted, unique approval digests, each tagged;
- `parent_semantic_root`: the parent's tagged spelling;
- `parent_derivation`: what kind of value the parent is.

The parent is typed. Generation zero has no semantic parent: its parent is the
bootstrap identity anchor (`BootstrapRoot`, `playbill-genesis-v1`), which enters
as `parent_derivation="bootstrap"`. Every later generation's parent is the
semantic root of the generation it succeeds (`SemanticRoot`), which enters as
`"playbill-sroot-v2"`. Both values share the `sha256:` spelling, so the
parent's spelling alone cannot say which it is; the discriminator does, and the
same 32-byte parent yields two different children under the two kinds.

This module is pure and imports nothing from the ledger or settlement, so
bootstrap, settlement, replay and checkpoint prefix re-derivation all share it.
"""

from __future__ import annotations

from typing import Final, Literal

from cruxible_client.contracts.canonical import (
    ApprovalDigest,
    BootstrapRoot,
    ChangeSetDigest,
    SemanticMerkleRoot,
    SemanticRoot,
    typed_digest,
)
from cruxible_client.contracts.errors import SettlementIntegrityError

SEMANTIC_ROOT_DOMAIN: Final = "playbill-sroot-v2"
BOOTSTRAP_PARENT: Final = "bootstrap"

ParentDerivation = Literal["bootstrap", "playbill-sroot-v2"]
SemanticParent = BootstrapRoot | SemanticRoot


def parent_derivation(parent: SemanticParent) -> ParentDerivation:
    """Name the kind of a semantic root's parent: the bootstrap anchor or a root."""

    if isinstance(parent, BootstrapRoot):
        return BOOTSTRAP_PARENT
    if isinstance(parent, SemanticRoot):
        return SEMANTIC_ROOT_DOMAIN
    raise SettlementIntegrityError(
        "a semantic root's parent is a bootstrap root or a semantic root"
    )


def compute_semantic_root(
    *,
    manifest_root: str,
    changeset_digest: str,
    approval_digests: tuple[str, ...],
    parent: SemanticParent,
) -> SemanticRoot:
    """Derive one generation's semantic root from tagged inputs and a typed parent.

    Each tagged input is parsed as its own digest kind, so a flat `sha256:`
    manifest root, or any other kind in any field, is refused rather than
    hashed.
    """

    derivation = parent_derivation(parent)
    manifest = SemanticMerkleRoot.from_tagged(manifest_root)
    changeset = ChangeSetDigest.from_tagged(changeset_digest)
    parsed_approvals = tuple(ApprovalDigest.from_tagged(value) for value in approval_digests)
    if approval_digests != tuple(sorted(set(approval_digests))):
        raise SettlementIntegrityError("semantic-root approval digests must be sorted and unique")
    return typed_digest(
        SemanticRoot,
        SEMANTIC_ROOT_DOMAIN,
        {
            "manifest_root": manifest.tagged,
            "changeset_digest": changeset.tagged,
            "approval_digests": [approval.tagged for approval in parsed_approvals],
            "parent_semantic_root": parent.tagged,
            "parent_derivation": derivation,
        },
    )


__all__ = [
    "BOOTSTRAP_PARENT",
    "SEMANTIC_ROOT_DOMAIN",
    "ParentDerivation",
    "SemanticParent",
    "compute_semantic_root",
    "parent_derivation",
]
