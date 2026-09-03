"""Claim Flow-A binding and Flow-B self-source lowering laws."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cruxible_client.contracts.authoring.models import (
    AuthoringExactContentObjectV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    ExactContentClaimObject,
    parse_claim,
)
from cruxible_core.playbill.authoring.lowering import lower_authoring
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _coordinator,
    _seed_claim_surface,
    _self_source_payload,
)
from tests.test_playbill.test_claims import _claim_type


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

    assert isinstance(claim, ClaimArtifactV2)
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
            "source": WorkingSelectionObservationV1(
                source_id=source_id,
                coordinate=WorkingDigestCoordinateV1(
                    source_content_digest="sha256:"
                    + hashlib.sha256(b"status: ready\n").hexdigest(),
                    source_byte_length=len(b"status: ready\n"),
                ),
                selected_content_base64=base64.b64encode(selected).decode("ascii"),
                selected_bytes_digest=selected_digest,
                selector=WorkingAnchorWindowV1(
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
    assert isinstance(claim, ClaimArtifactV2)
    assert [(item.role, item.origin) for item in claim.backing.citations] == [
        ("evidence", "independent")
    ]
    assert len(claim.backing.source_mappings[0].spans) == 1
    assert claim.backing.source_mappings[0].spans[0].content_digest == selected_digest


def test_exact_content_body_digest_and_span_are_daemon_derived(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    # The object-kind law admits an object only on the ClaimType kind that declares
    # it, and exact_content is its own first-class ClaimType.object_kind. The shared
    # fixture type declares a literal, so this flow seeds the exact_content type it
    # actually authors against.
    _seed_claim_surface(
        instance,
        owner,
        claim_type_override=_claim_type().model_copy(
            update={"object_kind": "exact_content", "literal_schema": None}
        ),
    )
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    body = b"A concise governed status explanation."
    payload = _self_source_payload().model_copy(
        update={
            "statement": _self_source_payload().statement.model_copy(
                update={
                    "object": AuthoringExactContentObjectV1(
                        content_base64=base64.b64encode(body).decode("ascii")
                    )
                }
            )
        }
    )
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

    assert isinstance(claim.statement.object, ExactContentClaimObject)
    assert claim.statement.object.content_digest == "sha256:" + hashlib.sha256(body).hexdigest()
    assert claim.statement.object.span is not None
    assert claim.statement.object.span.end_byte == len(body)
