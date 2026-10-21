"""Block depublish releases only blocks a registration names.

The insertion/publication road is cut: nothing prepares, confirms or abandons a
publication, and `block depublish` releases only blocks declared with
`block repin`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import FormatError
from cruxible_core.service.proposals.publications import service_depublish_playbill_block
from tests.core_support._support import initialize_local


def test_depublishing_a_block_no_registration_names_refuses_by_name(tmp_path: Path) -> None:
    """Releasing a block nothing registers refuses by name, and only by name."""

    instance, _owner = initialize_local(tmp_path)

    with pytest.raises(FormatError, match="cruxible.block.not_registered"):
        service_depublish_playbill_block(
            instance,
            source_id="repo.work-items",
            block_id="nothing-registers-this",
        )


def test_a_sync_read_says_whether_the_instance_still_registers_the_block(tmp_path: Path) -> None:
    """Detach strips a depublished block, so every sync read carries the registration fact."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.authoring.models import BlockSyncReadRequest
    from cruxible_client.contracts.declared_blocks import (
        ProjectionBlockStamp,
        ProjectionClaimBacking,
    )
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.authoring.registrations import (
        projection_block_registered,
        release_projection_block_declaration,
        write_projection_block_declaration,
    )
    from cruxible_core.service.authoring.projection_sync import (
        service_read_playbill_block_sync_backing,
    )

    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )
    stamp = ProjectionBlockStamp(
        source_id="corpus.runbook",
        block_id="summary",
        declared_generation=1,
        declared_coordinate=coordinate,
        backing=(
            ProjectionClaimBacking(
                identity=ArtifactIdentity(kind="Claim", name="CLM-" + "a" * 32),
                statement_digest="sha256:" + "8" * 64,
            ),
        ),
        body_digest="sha256:" + "9" * 64,
    )

    def read() -> bool | None:
        return service_read_playbill_block_sync_backing(
            instance, request=BlockSyncReadRequest(stamp=stamp)
        ).registered

    names = {"source_id": "corpus.runbook", "block_id": "summary"}
    assert read() is False and not projection_block_registered(instance, **names)
    write_projection_block_declaration(
        instance,
        **names,
        declared_generation=1,
        declared_coordinate=coordinate,
        declared_by="operator",
        declared_at="2026-10-21T00:00:00Z",
        stamp_digest="sha256:" + "5" * 64,
    )
    assert read() is True
    release_projection_block_declaration(instance, **names)
    assert read() is False
