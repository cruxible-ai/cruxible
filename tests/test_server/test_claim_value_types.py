"""``capture<*>`` and ``key<K>`` are ClaimType value types a Claim can hold.

Both pass ClaimType preflight, and on purpose: a ``capture<K>`` field holds a
Capture digest, and a ``key<K>`` (or ``key_part<K:p>``) field holds a canonical
key of kind ``K``, checked with that kind's identity rules when the Claim is
accepted, by authoring and by ``set`` alike. Each is written and read back here,
so a decision to refuse them at authoring has to start by changing this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.examples import subject_example, subject_kind_example
from cruxible_client.authoring.inputs import (
    ChangeSetInput,
    ClaimInput,
    ClaimTypeInput,
    LiteralObjectInput,
    SelfSourceInput,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.shapes import coerce_field_type
from cruxible_client.contracts.write import SetRequest
from cruxible_client.transport.http import CruxibleClient

DIGEST = "sha256:" + "a" * 64
#: field -> (value type, a value it holds)
FIELDS = {
    "project.work_item.attachment": ("capture<*>", DIGEST),
    "project.work_item.parent": ("key<project.work_item>", "replace-me"),
    "project.work_item.parent_slug": ("key_part<project.work_item:slug>", "replace-me"),
}


def _claim_type(predicate: str, value: str) -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=("project.work_item",),
        value=coerce_field_type(value),
        cardinality="one",
        permitted_roles=("normative",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one", eligible_verdicts=("supported",), selector="only_contender"
        ),
        default_role="normative",
        evidence_requirement="self",
        revision_evidence="replace",
    )


def _landed(transport: CruxibleClient, instance_id: str, payload: object) -> None:
    submitted = transport.submit_authoring_input(
        instance_id,
        input=payload.model_dump(mode="json"),  # type: ignore[attr-defined]
    )
    assert submitted.model_dump(mode="json")["outcome"]["status"] == "landed", submitted


@pytest.fixture
def work_item(
    playbill_http: tuple[TestClient, str, Path],
) -> tuple[CruxibleClient, str]:
    http, instance_id, _ = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    _landed(
        transport,
        instance_id,
        ChangeSetInput(
            kind="change_set",
            members=(
                *subject_kind_example().members,
                subject_example(),
                *(
                    ClaimTypeInput(kind="claim_type", claim_type=_claim_type(name, value))
                    for name, (value, _held) in FIELDS.items()
                ),
            ),
            rationale="A work item, and fields holding a Capture and keys.",
        ),
    )
    return transport, instance_id


def test_capture_and_key_valued_claims_are_written_and_read_back(
    work_item: tuple[CruxibleClient, str],
) -> None:
    transport, instance_id = work_item
    for predicate, (_value, held) in FIELDS.items():
        _landed(
            transport,
            instance_id,
            ClaimInput(
                kind="claim",
                subject="project.work_item/replace-me",
                predicate=predicate,
                object=LiteralObjectInput(kind="literal", value=held),
                role="normative",
                rationale="The value this field holds.",
                source=SelfSourceInput(kind="self_source", body=f"{predicate}: {held}\n"),
            ),
        )
    card = transport.get(instance_id, request=GetRequest(ref="project.work_item/replace-me")).card
    shown = {row["predicate"]: row["value"] for row in card.model_dump(mode="json")["claims"]}
    assert shown["attachment"] == DIGEST
    assert shown["parent"] == "replace-me"
    assert shown["parent_slug"] == "replace-me"


def test_set_writes_a_capture_valued_field(work_item: tuple[CruxibleClient, str]) -> None:
    transport, instance_id = work_item
    outcome = transport.set(
        instance_id,
        request=SetRequest(
            subject="project.work_item/replace-me",
            field="project.work_item.attachment",
            value=DIGEST,
            because="The attached file.",
        ),
    )
    assert outcome.status == "accepted", outcome


def test_set_writes_key_valued_fields(work_item: tuple[CruxibleClient, str]) -> None:
    """``set`` checks a key value with the kind's identity rules, as acceptance does."""

    transport, instance_id = work_item
    for field in ("project.work_item.parent", "project.work_item.parent_slug"):
        outcome = transport.set(
            instance_id,
            request=SetRequest(
                subject="project.work_item/replace-me",
                field=field,
                value="replace-me",
                because="Its parent item.",
            ),
        )
        assert outcome.status == "accepted", outcome
    # A key not in the kind's canonical form (its slug is casefolded) is refused.
    refused = transport.set(
        instance_id,
        request=SetRequest(
            subject="project.work_item/replace-me",
            field="project.work_item.parent",
            value="Replace-Me",
            because="Its parent item.",
        ),
    )
    assert refused.status == "refused"
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.write.value_type_mismatch"
    assert "canonical_form_failed" in refused.refusal.message
