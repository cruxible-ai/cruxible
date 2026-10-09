"""Re-observation: the same canonical statement on the same observed content is a no-op.

On new content the live Claim's evidence is refreshed by revising it; a second
Claim stating the same thing is never written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    ExistingCaptureCitationSource,
)
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.procedures.proposal_delivery import _restated_on_new_content
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from tests.core_support._identity_support import KIND, OWNER, seed_identity_surface
from tests.core_support._write_support import cited_captures

ALPHA = f"{KIND}/alpha"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_identity_surface(tmp_path)


def write(instance: PlaybillInstance, change: dict[str, Any], because: str) -> WriteOutcome:
    request = WriteRequest.model_validate({"because": because, "changes": [change]})
    return service_playbill_write(
        instance, request=request, caller=WriteCaller(actor=OWNER, may_activate=True)
    )


def test_restating_a_value_on_the_same_evidence_changes_nothing(instance: PlaybillInstance) -> None:
    first = write(
        instance, {"op": "set", "subject": ALPHA, "field": "code", "value": "x"}, "Read it."
    )
    claim = first.changes[0].claim
    assert claim is not None
    head = instance.accepted_coordinate().git_oid
    cited = cited_captures(instance, claim)

    again = write(
        instance, {"op": "set", "subject": ALPHA, "field": "code", "value": " X"}, "Read it."
    )
    assert again.status == "accepted" and again.proposal is None, again
    assert instance.accepted_coordinate().git_oid == head
    assert cited_captures(instance, claim) == cited

    refreshed = write(
        instance, {"op": "set", "subject": ALPHA, "field": "code", "value": "x"}, "Read it again."
    )
    assert refreshed.status == "accepted" and refreshed.proposal is not None, refreshed
    assert refreshed.changes[0].claim == claim
    assert cited_captures(instance, claim) > cited


def _restating(capture: str) -> ClaimAuthoringPayload:
    return ClaimAuthoringPayload(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(KIND, "alpha")),
            predicate=f"{KIND}.tags",
            object=LiteralClaimObject(value="infra"),
            role="observation",
        ),
        rationale="The run read it.",
        source=ExistingCaptureCitationSource(capture_digest=capture),
        citation_role="evidence",
        dependency_drafts=ClaimDependencyDrafts(),
    )


def test_a_terminal_item_restating_a_live_claim_on_new_content_revises_it(
    instance: PlaybillInstance,
) -> None:
    held = write(instance, {"op": "add", "subject": ALPHA, "field": "tags", "value": "Infra"}, "A.")
    other = write(instance, {"op": "set", "subject": ALPHA, "field": "plain", "value": "y"}, "B.")
    claim, unrelated = held.changes[0].claim, other.changes[0].claim
    assert claim is not None and unrelated is not None
    (same_content,) = cited_captures(instance, claim)
    (new_content,) = cited_captures(instance, unrelated)
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)

    assert _restated_on_new_content(instance, tree, _restating(new_content)) == claim
    # On content the live Claim already cites the item is left as authored.
    assert _restated_on_new_content(instance, tree, _restating(same_content)) is None


def test_add_restating_a_live_value_refreshes_new_content_on_its_lineage(
    instance: PlaybillInstance,
) -> None:
    """Review F-005: add goes through the shared re-observation comparison."""

    change = {"op": "add", "subject": ALPHA, "field": "tags", "value": "Infra"}
    first = write(instance, change, "A")
    claim = first.changes[0].claim
    assert claim is not None
    before = cited_captures(instance, claim)

    same = write(instance, {**change, "value": " INFRA "}, "A")
    assert same.status == "accepted" and same.proposal is None
    assert same.changes[0].already_live and same.changes[0].claim == claim
    assert cited_captures(instance, claim) == before

    refreshed = write(instance, {**change, "value": " INFRA "}, "B")
    assert refreshed.status == "accepted" and refreshed.proposal is not None, refreshed
    assert not refreshed.changes[0].already_live
    assert refreshed.changes[0].claim == claim
    assert cited_captures(instance, claim) > before

    absent = write(instance, {**change, "expect_absent": True}, "C")
    assert absent.refusal is not None
    assert absent.refusal.code == "cruxible.write.value_already_present"
