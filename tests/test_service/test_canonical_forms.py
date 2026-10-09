"""A declared canonical form is applied on write in every lowering path (ClaimType v8).

The stored literal is always canonical; the raw spelling stays in the evidence.
Enum membership is checked after canonicalization, and a ClaimType without a
form stores exactly what was written.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDrafts,
    SelfSourceBody,
)
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.authoring import lowering
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.discovery.query_values import read_live_values
from tests.core_support._identity_support import KIND, OWNER, seed_identity_surface

ALPHA = f"{KIND}/alpha"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_identity_surface(tmp_path)


def write(instance: PlaybillInstance, *changes: dict[str, Any], **options: Any) -> WriteOutcome:
    request = WriteRequest.model_validate(
        {"because": "The writer read it.", "changes": list(changes), **options}
    )
    return service_playbill_write(
        instance, request=request, caller=WriteCaller(actor=OWNER, may_activate=True)
    )


def stored(instance: PlaybillInstance, field: str, subject: str = "alpha") -> list[object]:
    return sorted(
        str(item.value)
        for item in read_live_values(
            instance,
            instance.accepted_coordinate(),
            subject_paths=(subject_path(KIND, subject),),
            predicates=(f"{KIND}.{field}",),
        )
    )


def _member(value: object, field: str = "code") -> ClaimAuthoringPayloadV2:
    return ClaimAuthoringPayloadV2(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(KIND, "alpha")),
            predicate=f"{KIND}.{field}",
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        rationale="Authored by hand.",
        source=SelfSourceBody(content_base64=base64.b64encode(b"raw spelling").decode()),
        dependency_drafts=ClaimDependencyDrafts(),
    )


def test_a_write_stores_the_canonical_value(instance: PlaybillInstance) -> None:
    outcome = write(
        instance, {"op": "set", "subject": ALPHA, "field": "code", "value": "  Pkg  ONE "}
    )
    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].after == "pkg one"
    assert stored(instance, "code") == ["pkg one"]


def test_enum_membership_is_checked_after_canonicalization(instance: PlaybillInstance) -> None:
    accepted = write(instance, {"op": "set", "subject": ALPHA, "field": "state", "value": " OPEN "})
    assert accepted.status == "accepted", accepted
    assert stored(instance, "state") == ["open"]
    refused = write(instance, {"op": "set", "subject": ALPHA, "field": "state", "value": "Opened"})
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.write.value_not_member"


def test_a_claim_type_without_a_form_stores_exactly_what_was_written(
    instance: PlaybillInstance,
) -> None:
    outcome = write(
        instance, {"op": "set", "subject": ALPHA, "field": "plain", "value": " Raw  Text"}
    )
    assert outcome.status == "accepted", outcome
    assert stored(instance, "plain") == [" Raw  Text"]


def test_a_change_set_member_is_lowered_to_the_canonical_value(instance: PlaybillInstance) -> None:
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    submitted = coordinator.compile_and_submit(
        actor=OWNER,
        payload=ChangeSetAuthoringPayload(members=(_member("ＰKG  Two"),)),
        canonical_timestamp="2026-10-09T12:00:00.000000Z",
    )
    assert submitted.status.proposal_id is not None, submitted
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    assert stored(instance, "code") == ["pkg two"]


def test_the_claim_law_refuses_a_literal_that_is_not_canonical(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lowering that forgot the form: the Claim law still holds the line.
    monkeypatch.setattr(lowering, "_canonical_object", lambda value, _claim_type: value)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    result = coordinator.compile(
        actor=OWNER,
        payload=ChangeSetAuthoringPayload(members=(_member(" Not Canonical"),)),
        canonical_timestamp="2026-10-09T12:00:00.000000Z",
    )
    codes = [item.code for item in result.frontier.diagnostics]
    assert "cruxible.claim.literal_not_canonical" in codes, codes
