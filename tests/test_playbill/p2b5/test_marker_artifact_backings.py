"""ClaimType/Subject marker backing succession and compiler-pinned block sync."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.test_playbill._knowledge_loop_support import TIMESTAMP
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_query_execution_service import _instance_with_query
from tests.test_playbill.test_resolution_contracts import _accept_tree

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.models import PlaybillBlockSyncReadRequestV1
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionArtifactBackingV1,
    ProjectionBlockStampV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.service.playbill_projection_sync import (
    service_read_playbill_block_sync_backing,
)


def test_artifact_backing_accepts_only_governed_vocabulary_and_entity_kinds() -> None:
    for kind, name in (
        ("ClaimType", "sec.vuln.severity"),
        ("Subject", "sec.vulnerability/cve-2026-69247"),
    ):
        backing = ProjectionArtifactBackingV1(
            identity=ArtifactIdentity(kind=kind, name=name),
            artifact_digest="sha256:" + "a" * 64,
        )
        assert ProjectionArtifactBackingV1.model_validate_json(backing.model_dump_json()) == backing

    with pytest.raises(ValidationError, match="ClaimType or Subject"):
        ProjectionArtifactBackingV1(
            identity=ArtifactIdentity(kind="Claim", name="CLM-" + "a" * 32),
            artifact_digest="sha256:" + "a" * 64,
        )


def test_claim_type_block_sync_renders_current_artifact_with_compiler_pin(
    tmp_path: Path,
) -> None:
    instance, owner = _instance_with_query(tmp_path)
    predicate = "sec.vuln.severity"
    claim_type = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
            "allowed_subject_kinds": ("sec.vulnerability",),
            "literal_schema": {"enum": ["high", "low", "medium"], "type": "string"},
        }
    )
    path = claim_type_path(predicate)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[path] = render_claim_type(claim_type)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=TIMESTAMP,
        proposal_name="seed-sync-vulnerability-severity-vocabulary",
    )
    declared = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    original_digest = claim_type_digest(claim_type).tagged
    stamp = ProjectionBlockStampV1(
        source_id="corpus.ontology",
        block_id="severity-vocabulary",
        declared_generation=instance.accepted_history()[-1].sequence,
        declared_coordinate=declared,
        backing=(
            ProjectionArtifactBackingV1(
                identity=claim_type.identity,
                artifact_digest=original_digest,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(b"old vocabulary\n").hexdigest(),
    )

    successor = claim_type.model_copy(
        update={
            "literal_schema": {
                "enum": ["critical", "high", "low", "medium"],
                "type": "string",
            },
            "lifecycle": ArtifactLifecycle(predecessor_digest=original_digest),
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[path] = render_claim_type(successor)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=TIMESTAMP,
        proposal_name="migrate-sync-vulnerability-severity-vocabulary",
    )

    result = service_read_playbill_block_sync_backing(
        instance,
        request=PlaybillBlockSyncReadRequestV1(stamp=stamp),
    )

    assert result.status == "successor"
    assert result.artifact_digest == claim_type_digest(successor).tagged
    # Nothing renders. The read used to hand back the candidate card this block
    # would be rewritten to; a projection block is prose an agent wrote, so the
    # only thing accepted state can say about a governed vocabulary the block
    # holds is that it moved, and the current spelling to re-stamp it with.
    assert result.body is None
    assert result.body_digest is None
    assert isinstance(result.backing, ProjectionArtifactBackingV1)
    assert result.backing.artifact_digest == result.artifact_digest
    assert result.moved_backings == (result.backing,)
