"""PB-E local source-catalog compilation, alignment, and frozen proposal tests."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.source_catalog import (
    SourceCatalog,
    SourceCatalogEntry,
    merge_source_catalogs,
)
from cruxible_core.service.playbill_documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_source_catalog import (
    service_check_playbill_source_bundle,
    service_compile_playbill_sources,
    service_propose_playbill_source_bundle,
)
from tests.test_playbill.test_activation import _sign
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance


def _entry(*, locator: str = "specs/design.md", root_alias: str | None = None):
    return SourceCatalogEntry(
        name="playbill-design",
        locator=locator,
        root_alias=root_alias,
        document_id="design",
        document_kind="design",
        title="Playbill design",
        media_type="text/markdown",
        required_tier="graph_write",
        approval_roles=("owner",),
        governance_scope=("project:playbill",),
    )


def _catalog(entry: SourceCatalogEntry, *, kind: str = "portable") -> SourceCatalog:
    return SourceCatalog(catalog_kind=kind, entries=(entry,))  # type: ignore[arg-type]


def test_compile_is_read_only_and_propose_uses_frozen_bytes(tmp_path: Path) -> None:
    instance, owner, _reviewer = _instance(tmp_path)
    repository = tmp_path / "authoring"
    source = repository / "specs" / "design.md"
    source.parent.mkdir(parents=True)
    original = b"# Playbill v1\n"
    source.write_bytes(original)
    catalog = _catalog(_entry())

    bundle = service_compile_playbill_sources(
        instance,
        catalog=catalog,
        repository_root=repository,
    )
    assert instance.body_store().verify(bundle.documents[0].source.body_digest) is False
    assert (
        service_check_playbill_source_bundle(
            instance,
            bundle=bundle,
        )
        .alignments[0]
        .state
        == "untracked"
    )
    assert "authoring" not in bundle.model_dump_json()
    assert "specs/design.md" not in bundle.model_dump_json()

    source.write_bytes(b"# Playbill changed after compile\n")
    proposed = service_propose_playbill_source_bundle(
        instance,
        bundle=bundle,
        source_name="playbill-design",
        actor_id="owner",
        proposal_name="catalog-design",
        timestamp=TIMESTAMP,
    ).proposal
    assert proposed.candidate is not None
    stored = instance.body_store().read(
        bundle.documents[0].source.body_digest,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert stored == original
    assert base64.b64decode(bundle.documents[0].body_base64) == original
    assert (
        service_check_playbill_source_bundle(
            instance,
            bundle=bundle,
        )
        .alignments[0]
        .state
        == "pending"
    )

    approval = _sign(
        owner,
        proposed.candidate.candidate_digest,
        proposed.candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposed.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="relay",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=proposed.admission.proposal_id,
    )
    assert (
        service_check_playbill_source_bundle(
            instance,
            bundle=bundle,
        )
        .alignments[0]
        .state
        == "aligned"
    )
    modified = service_compile_playbill_sources(
        instance,
        catalog=catalog,
        repository_root=repository,
    )
    assert service_check_playbill_source_bundle(instance, bundle=modified).alignments[0].state == (
        "modified"
    )
    saved_manifest = instance.proposal_evidence().read_source_compilation(
        bundle.manifest.compilation_digest
    )
    assert saved_manifest == bundle.manifest

    second = service_propose_playbill_source_bundle(
        instance,
        bundle=modified,
        source_name="playbill-design",
        actor_id="owner",
        proposal_name="catalog-design-v2",
        timestamp=TIMESTAMP,
    ).proposal
    assert second.candidate is not None
    second_approval = _sign(
        owner,
        second.candidate.candidate_digest,
        second.candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=second.admission.proposal_id,
        attestation=second_approval.attestation,
        authenticated_submitter="relay",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=second.admission.proposal_id,
    )
    assert (
        service_check_playbill_source_bundle(instance, bundle=bundle).alignments[0].state
        == "behind"
    )


def test_catalog_merge_and_path_guards_refuse_ambiguity_and_escape(tmp_path: Path) -> None:
    portable = _catalog(_entry())
    local_root = tmp_path / "private-specs"
    local_root.mkdir()
    (local_root / "design.md").write_text("# Local\n")
    local = _catalog(
        _entry(locator="design.md", root_alias="private"),
        kind="local",
    )
    merged = merge_source_catalogs(portable, local)
    assert merged.entries[0].root_alias == "private"

    instance_root = tmp_path / "instance"
    instance_root.mkdir()
    instance, _owner, _reviewer = _instance(instance_root)
    bundle = service_compile_playbill_sources(
        instance,
        catalog=merged,
        repository_root=tmp_path,
        root_aliases={"private": local_root},
    )
    assert bundle.documents[0].source.public_uri is None

    conflicting = _catalog(
        _entry(locator="design.md", root_alias="private").model_copy(
            update={"document_id": "other"}
        ),
        kind="local",
    )
    with pytest.raises(PlaybillFormatError, match="ambiguously"):
        merge_source_catalogs(portable, conflicting)

    symlink = local_root / "linked.md"
    symlink.symlink_to(local_root / "design.md")
    with pytest.raises(PlaybillFormatError, match="symlink"):
        service_compile_playbill_sources(
            instance,
            catalog=_catalog(
                _entry(locator="linked.md", root_alias="private"),
                kind="local",
            ),
            repository_root=tmp_path,
            root_aliases={"private": local_root},
        )
    with pytest.raises(ValueError, match="normalized"):
        _entry(locator="../escape.md")
    for local_uri in ("file:///tmp/private.md", "/tmp/private.md"):
        with pytest.raises(ValueError, match="non-file URI"):
            SourceCatalogEntry(
                **{
                    **_entry().model_dump(mode="python"),
                    "public_uri": local_uri,
                }
            )
