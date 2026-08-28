"""PC-A1 shared artifact kernel and fail-closed registry tests."""

from __future__ import annotations

import re

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactKindRegistry,
    ArtifactLifecycle,
    ArtifactPathKind,
    ArtifactPin,
    GovernedArtifactProtocol,
    parse_artifact_identity,
)
from cruxible_client.contracts.documents import (
    DocumentArtifactAdapter,
    DocumentAuthority,
    DocumentLifecycle,
    DocumentPin,
    DocumentShell,
    document_digest,
    render_document,
)
from cruxible_client.contracts.errors import ProjectionFormatError


def test_artifact_value_objects_use_generic_kind_qualified_identity() -> None:
    identity = ArtifactIdentity(kind="Subject", name="project.work_item/wi-123")
    assert identity.qualified == "Subject:project.work_item/wi-123"
    assert parse_artifact_identity(identity.qualified) == identity
    lifecycle = ArtifactLifecycle()
    pin = ArtifactPin(
        role="contract",
        target=ArtifactIdentity(kind="Contract", name="project.work_item"),
        artifact_digest="sha256:" + "11" * 32,
    )
    assert lifecycle.state == "live"
    assert pin.target.qualified == "Contract:project.work_item"


def test_document_adapter_does_not_change_frozen_document_bytes_or_digest() -> None:
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title="Playbill design",
        media_type="text/markdown",
        body_digest="sha256:" + "22" * 32,
        pins=(
            DocumentPin(
                role="reference",
                target_identity="document:program",
                target_digest="sha256:" + "33" * 32,
            ),
        ),
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    wire_before = render_document(shell)
    digest_before = document_digest(shell)
    adapter = DocumentArtifactAdapter(shell)

    assert isinstance(adapter, GovernedArtifactProtocol)
    assert adapter.identity == ArtifactIdentity(kind="document", name="design")
    assert adapter.pins[0].target == ArtifactIdentity(kind="document", name="program")
    assert adapter.lifecycle.predecessor_digest is None
    assert render_document(shell) == wire_before
    assert document_digest(shell) == digest_before


def test_artifact_kind_reservations_and_unknown_paths_refuse() -> None:
    registry = ArtifactKindRegistry(
        (ArtifactPathKind("document", re.compile(r"^documents/[a-z]+\.yaml$")),)
    )
    reserved = registry.reserve(
        kind="claim-type",
        path_pattern=r"^claim-types/[a-z]+/[a-z]+\.yaml$",
    )
    assert reserved.reserved_kinds() == ("claim-type",)
    with pytest.raises(ProjectionFormatError, match="reserved but unimplemented"):
        reserved.resolve_path("claim-types/project/status.yaml")
    with pytest.raises(ProjectionFormatError, match="no registered"):
        reserved.resolve_path("unknown/value.yaml")

    activated = reserved.activate(kind="claim-type")
    assert activated.resolve_path("claim-types/project/status.yaml") == "claim-type"
