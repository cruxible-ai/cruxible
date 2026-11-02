"""Claim Flow-A binding and Flow-B self-source lowering laws."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringExactContentObject,
    WorkingAnchorWindow,
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    parse_claim,
)
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import (
    TIMESTAMP,
    _coordinator,
    _seed_claim_surface,
    _self_source_payload,
)


def test_flow_b_lowers_to_retained_copy_self_source_without_writer_digests(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _self_source_payload()
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    lowered = lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    claim_path = next(
        path for path, _content in lowered.changed_members if path.startswith("claims/")
    )
    claim = parse_claim(lowered.proposed_tree[claim_path], path=claim_path)

    assert isinstance(claim, ClaimArtifact)
    assert len(claim.backing.citations) == 1
    citation = claim.backing.citations[0]
    assert (citation.role, citation.origin) == ("copy", "self_source")
    assert instance.body_store().verify(citation.capture_digest)
    assert "digest" not in payload.model_dump_json()


def test_flow_a_binds_only_the_selection_and_can_pass_existing_claim_laws(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    source_id = "repo.work-items"
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract(source_id),
    )
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    selected = b"status: ready"
    selected_digest = "sha256:" + hashlib.sha256(selected).hexdigest()
    payload = _self_source_payload().model_copy(
        update={
            "source": WorkingSelectionObservation(
                source_id=source_id,
                coordinate=WorkingDigestCoordinate(
                    source_content_digest="sha256:"
                    + hashlib.sha256(b"status: ready\n").hexdigest(),
                    source_byte_length=len(b"status: ready\n"),
                ),
                selected_content_base64=base64.b64encode(selected).decode("ascii"),
                selected_bytes_digest=selected_digest,
                selector=WorkingAnchorWindow(
                    anchor="status: ready",
                    start_byte=0,
                    end_byte=len(selected),
                    observed_occurrence_count=1,
                ),
            ),
            "citation_role": "evidence",
        }
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert result.verdict == "passed"
    assert result.frontier.diagnostics == ()
    lowered = lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    claim_path = next(
        path for path, _content in lowered.changed_members if path.startswith("claims/")
    )
    claim = parse_claim(lowered.proposed_tree[claim_path], path=claim_path)
    assert isinstance(claim, ClaimArtifact)
    assert [(item.role, item.origin) for item in claim.backing.citations] == [
        ("evidence", "independent")
    ]
    assert len(claim.backing.source_mappings[0].spans) == 1
    assert claim.backing.source_mappings[0].spans[0].content_digest == selected_digest


def test_exact_content_claim_input_refuses_with_document_repair(tmp_path: Path) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    payload = _self_source_payload().model_copy(
        update={
            "statement": _self_source_payload().statement.model_copy(
                update={
                    "object": AuthoringExactContentObject(
                        content_base64=base64.b64encode(b"Prose.").decode()
                    )
                }
            )
        }
    )
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    with pytest.raises(
        AuthoringLoweringError, match="Prose lives in Documents; cite a Document section"
    ) as refused:
        lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    assert refused.value.code == "cruxible.claim.exact_content_retired"
