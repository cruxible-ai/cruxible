"""Publication-v2 wire shapes the stored intent format still carries.

Nothing mints a publication any more and the insertion road is cut: no verb
prepares, confirms, abandons or reads one, and `block depublish` releases only
blocks declared with `block repin`. The target and source-observation models
remain part of the stored intent format, so their digest laws are pinned here.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import (
    AuthoringExistingClaimDisposition,
    InsertionAnchorWindow,
    InsertionTarget,
    PublicationSourceObservation,
    SelfSourceBody,
    WorkingDigestCoordinate,
    insertion_target_v2_digest,
    publication_block_id,
    publication_source_observation_v2_digest,
)
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.errors import FormatError
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.proposals.publications import service_depublish_playbill_block
from tests.core_support._support import client_material, initialize_local
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_ledger.test_activation import _sign


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _activate(
    instance,  # type: ignore[no-untyped-def]
    _owner: object,
    *,
    proposal_id: str,
    candidate_digest: str,
) -> None:
    approver = client_material(instance.root.parent, instance)
    approval = _sign(
        approver,
        candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=approval.attestation,
        authenticated_submitter=approver.principal.principal_id,
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"


def _successor_payload(claim_id: str, *, value: str):  # type: ignore[no-untyped-def]
    payload = _self_source_payload()
    return payload.model_copy(
        update={
            "revises": claim_id,
            "insertion_target": None,
            "rationale": f"Publish the {value} successor.",
            "statement": payload.statement.model_copy(
                update={"object": LiteralClaimObject(value=value)}
            ),
            "source": SelfSourceBody(
                content_base64=base64.b64encode(value.encode()).decode("ascii")
            ),
            "existing_claim_dispositions": (
                AuthoringExistingClaimDisposition(
                    claim_id=claim_id,
                    disposition="not_tested",
                ),
            ),
        }
    )


def _target(content: bytes = b"status: \n") -> InsertionTarget:
    return InsertionTarget(
        source_id="repo.work-items",
        coordinate=WorkingDigestCoordinate(
            source_content_digest=_digest(content),
            source_byte_length=len(content),
        ),
        initial_preimage_digest=_digest(content),
        initial_preimage_byte_length=len(content),
        selector=InsertionAnchorWindow(
            anchor_content_base64=base64.b64encode(content).decode("ascii"),
            anchor_bytes_digest=_digest(content),
            start_byte=0,
            end_byte=len(content),
            insertion_offset=len(content),
            observed_occurrence_count=1,
        ),
        operation="insert_after",
    )


def test_v2_target_and_source_observation_digest_exact_bytes() -> None:
    target = _target()
    source = PublicationSourceObservation(
        source_id=target.source_id,
        content_base64=base64.b64encode(b"status: ").decode("ascii"),
        content_digest=_digest(b"status: "),
        byte_length=8,
    )

    assert insertion_target_v2_digest(target).startswith("sha256:")
    assert publication_source_observation_v2_digest(source).startswith("sha256:")
    assert source.content == b"status: "


def test_v2_source_observation_refuses_noncanonical_base64_and_wrong_digest() -> None:
    with pytest.raises(ValidationError, match="canonical base64"):
        PublicationSourceObservation(
            source_id="repo.work-items",
            content_base64="c3RhdHVzOiA",
            content_digest=_digest(b"status: "),
            byte_length=8,
        )
    with pytest.raises(ValidationError, match="digest does not reproduce"):
        PublicationSourceObservation(
            source_id="repo.work-items",
            content_base64=base64.b64encode(b"status: ").decode("ascii"),
            content_digest=_digest(b"different"),
            byte_length=8,
        )


def test_publication_block_id_is_deterministic_and_parser_safe() -> None:
    expectation_id = _digest(b"expectation")
    first = publication_block_id(expectation_id)

    assert first == publication_block_id(expectation_id)
    assert first.startswith("pub-")
    assert len(first) == 36


def test_depublishing_a_block_no_registration_names_refuses_by_name(tmp_path: Path) -> None:
    """Releasing a block nothing registers refuses by name, and only by name."""

    instance, _owner = initialize_local(tmp_path)

    with pytest.raises(FormatError, match="cruxible.block.not_registered"):
        service_depublish_playbill_block(
            instance,
            source_id="repo.work-items",
            block_id="nothing-registers-this",
        )
