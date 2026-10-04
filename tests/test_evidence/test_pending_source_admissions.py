"""Uncommitted evaluation records are not pending or resolving proposals."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.source_catalog import SourceCatalog, SourceCatalogEntry
from cruxible_client.contracts.subjects import subject_digest, subject_path
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.discovery import curation as playbill_curation
from cruxible_core.service.evidence import source_catalog
from tests.core_support._candidate_support import submit_subject_candidate
from tests.core_support._knowledge_loop_support import accept_proposal, subject_shell
from tests.core_support._support import initialize_local
from tests.test_ledger.test_activation import _sign
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance


def _orphan(instance, proposal_id: str) -> None:  # type: ignore[no-untyped-def]
    # An evaluation can survive an interrupted submission before admission,
    # the publication commit point; drop the admission to reproduce that.
    evidence = instance.proposal_evidence()
    (evidence.proposals / f"{proposal_id.removeprefix('sha256:')}.json").unlink()


@pytest.mark.parametrize(
    ("admitted", "settled", "pending"),
    [(False, False, False), (True, False, True), (True, True, False)],
)
def test_pending_documents_require_an_unsettled_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    admitted: bool,
    settled: bool,
    pending: bool,
) -> None:
    instance, _owner, reviewer = _instance(tmp_path)
    repository = tmp_path / "authoring"
    source = repository / "specs" / "design.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# Cruxible v1\n")
    catalog = SourceCatalog(
        catalog_kind="portable",
        entries=(
            SourceCatalogEntry(
                name="playbill-design",
                locator="specs/design.md",
                document_id="design",
                document_kind="design",
                title="Cruxible design",
                media_type="text/markdown",
                required_tier="graph_write",
                governance_scope=("project:playbill",),
            ),
        ),
    )
    bundle = source_catalog.service_compile_playbill_sources(
        instance, catalog=catalog, repository_root=repository
    )
    proposed = source_catalog.service_propose_playbill_source_bundle(
        instance,
        bundle=bundle,
        source_name="playbill-design",
        actor_id="owner",
        proposal_name="catalog-design",
        timestamp=TIMESTAMP,
    ).proposal
    assert proposed.candidate is not None
    if settled:
        service_submit_playbill_approval(
            instance,
            proposal_id=proposed.admission.proposal_id,
            attestation=_sign(
                reviewer,
                proposed.candidate.candidate_digest,
                proposed.candidate.candidate.parent_semantic_root,
            ).attestation,
            authenticated_submitter="relay",
        )
        service_activate_playbill_proposal(
            instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
        )
    if not admitted:
        _orphan(instance, proposed.admission.proposal_id)
    evidence_type = type(instance.proposal_evidence())
    read_candidate = Mock(wraps=evidence_type.read_candidate)
    monkeypatch.setattr(
        evidence_type, "read_candidate", lambda store, digest: read_candidate(store, digest)
    )

    result = source_catalog._pending_body_digests(instance, instance.accepted_coordinate())

    body = bundle.documents[0].source.body_digest
    assert result == ({"design": {body}} if pending else {})
    if pending:
        read_candidate.assert_called_once()
        assert read_candidate.call_args.args[1] == proposed.candidate.candidate_digest
    else:
        read_candidate.assert_not_called()


@pytest.mark.parametrize("admitted", [False, True])
def test_curation_never_names_an_orphan_evaluation_as_the_resolving_proposal(
    tmp_path: Path,
    admitted: bool,
) -> None:
    instance, owner = initialize_local(tmp_path)
    initial = subject_shell("wi-dead")
    accept_proposal(
        instance,
        owner,
        submit_subject_candidate(
            instance,
            shell=initial,
            actor_id="owner",
            proposal_name="dead-subject-initial",
            timestamp="2026-08-26T18:00:00.000000Z",
        ),
    )
    retired = initial.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=subject_digest(initial).tagged,
            )
        }
    )
    # Both proposals evaluate to the accepted candidate. The orphan sorts
    # first, so selecting from raw evaluations would misattribute resolution.
    submissions = [
        submit_subject_candidate(
            instance,
            shell=retired,
            actor_id="owner",
            proposal_name=f"dead-subject-retirement-{number}",
            timestamp="2026-08-26T18:01:00.000000Z",
        )
        for number in (1, 2)
    ]
    candidates = {item.proposal.candidate.candidate_digest for item in submissions}
    assert len(candidates) == 1
    orphan, survivor = sorted(
        (item.proposal.admission.proposal_id for item in submissions),
        key=lambda value: value.encode("ascii"),
    )
    accept_proposal(instance, owner, submissions[0])
    _orphan(instance, orphan)
    if not admitted:
        _orphan(instance, survivor)

    path = subject_path(retired.subject_kind, retired.subject_id)
    item = Mock(
        item_id="item",
        first_proposed_generation=1,
        subject=initial.identity,
        latest_evidence_refs=(Mock(path=path),),
    )
    resolved = playbill_curation._accepted_retirements_for_items(instance, (item,))

    if admitted:
        assert set(resolved) == {"item"}
        _generation, proposal_id, record, affected = resolved["item"]
        assert proposal_id == survivor
        assert record.candidate_digest in candidates
        assert any(member.path == path and member.disposition == "retire" for member in affected)
    else:
        assert resolved == {}
