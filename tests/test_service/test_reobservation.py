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
from cruxible_core.procedures.proposal_delivery import _restated_lineage
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
        instance,
        request=request,
        caller=WriteCaller(actor=OWNER, may_activate=True, may_approve=True),
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


def test_a_terminal_item_restating_a_live_claim_targets_its_lineage(
    instance: PlaybillInstance,
) -> None:
    held = write(instance, {"op": "add", "subject": ALPHA, "field": "tags", "value": "Infra"}, "A.")
    other = write(instance, {"op": "set", "subject": ALPHA, "field": "plain", "value": "y"}, "B.")
    claim, unrelated = held.changes[0].claim, other.changes[0].claim
    assert claim is not None and unrelated is not None
    (same_content,) = cited_captures(instance, claim)
    (new_content,) = cited_captures(instance, unrelated)
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)

    # New content or content it already cites: either way the live Claim's
    # lineage, never a second Claim. Lowering decides refresh versus no-op.
    assert _restated_lineage(tree, _restating(new_content)) == claim
    assert _restated_lineage(tree, _restating(same_content)) == claim


def test_procedure_same_content_does_not_mint_duplicate(tmp_path: Path) -> None:
    """Review F-004 probe: a same-content restatement lowers into no new Claim."""

    from cruxible_client.contracts.authoring.models import (
        AuthoringExistingClaimDisposition,
        ChangeSetAuthoringPayload,
    )
    from cruxible_client.contracts.claims import parse_claim
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from tests.test_authoring.test_authoring_existing_capture import shared_capture_world
    from tests.test_authoring.test_authoring_preflight import _working_payload

    world, _owner, actor, first_id, _second, coordinator, _other = shared_capture_world(tmp_path)
    (capture,) = cited_captures(world, first_id)
    tree = world.immutable_tree_at(world.accepted_coordinate().git_oid)
    member = ClaimAuthoringPayload(
        statement=_working_payload(occurrence_count=1).statement,
        rationale="Restatement.",
        source=ExistingCaptureCitationSource(capture_digest=capture),
        citation_role="evidence",
        dependency_drafts=ClaimDependencyDrafts(),
        existing_claim_dispositions=(
            AuthoringExistingClaimDisposition(claim_id=first_id, disposition="not_tested"),
        ),
    )
    lineage = _restated_lineage(tree, member)
    assert lineage == first_id
    fresh = AuthoringIntentCoordinator(
        instance=world, store=coordinator.store, claim_id_factory=lambda: "CLM-" + "4" * 32
    )
    _intent, computed = fresh.preview(
        actor=actor,
        payload=ChangeSetAuthoringPayload(
            members=(member.model_copy(update={"revises": lineage}),)
        ),
        canonical_timestamp="2026-08-21T12:03:00.000000Z",
    )
    assert computed.lowered is not None, [
        item.model_dump() for item in computed.result.frontier.diagnostics
    ]
    new_claims = [
        parse_claim(body, path=path)
        for path, body in computed.lowered.changed_members
        if path.startswith("claims/") and path not in tree
    ]
    assert not new_claims
    assert not computed.lowered.changed_members


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


def test_a_terminal_item_naming_an_alias_finds_the_live_lineage(
    instance: PlaybillInstance,
) -> None:
    from cruxible_core.authoring.lowering import claim_aliases_resolved

    held = write(instance, {"op": "add", "subject": ALPHA, "field": "tags", "value": "Infra"}, "A.")
    claim = held.changes[0].claim
    assert claim is not None
    aliased = write(
        instance, {"op": "add", "subject": ALPHA, "field": "semantic.alias", "value": "a-one"}, "A."
    )
    assert aliased.status == "accepted"
    (capture,) = cited_captures(instance, claim)
    base = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(base.git_oid)
    by_alias = _restating(capture).model_copy(
        update={
            "statement": _restating(capture).statement.model_copy(
                update={"subject": SemanticAddress.whole_artifact(subject_path(KIND, "a-one"))}
            )
        }
    )
    assert _restated_lineage(tree, by_alias) is None
    assert _restated_lineage(tree, claim_aliases_resolved(instance, base, tree, by_alias)) == claim


def test_an_unchanged_receipt_child_never_settles_into_a_target() -> None:
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from cruxible_core.procedures.egress import (
        PreparedTerminalEgressV1,
        TerminalEgressChildReceiptV2,
        TerminalEgressChildReceiptV3,
        TerminalEgressReceiptV3,
        TerminalEgressUnchangedReceiptV1,
    )

    digest = "sha256:" + "a" * 64
    kept = TerminalEgressChildReceiptV3(
        child_index=0,
        item_key="k0",
        egress_digest=digest,
        path="claims/aa/CLM-" + "a" * 32 + ".json",
        reobserving_capture_digest=digest,
        reobserved_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    written = TerminalEgressChildReceiptV2(
        child_index=1,
        item_key="k1",
        egress_digest=digest,
        path="claims/bb/CLM-" + "b" * 32 + ".json",
    )
    common = {
        "kind": "propose_change_set",
        "run_id": "run-1",
        "node_id": "propose",
        "disposition": "received",
        "operation_key": digest,
        "proposal_id": "sha256:" + "1" * 64,
        "candidate_digest": "sha256:" + "2" * 64,
    }
    receipt = TerminalEgressReceiptV3(
        **common, children=(kept, written), target_paths=(written.path,)
    )
    assert TerminalEgressReceiptV3.model_validate_json(receipt.model_dump_json()) == receipt
    with pytest.raises(ValidationError):
        TerminalEgressReceiptV3(
            **common,
            children=(kept, written),
            target_paths=tuple(sorted((kept.path, written.path))),
        )
    nothing = TerminalEgressUnchangedReceiptV1(
        kind="settle_change_set", run_id="run-1", node_id="settle", children=(kept,)
    )
    assert nothing.disposition == "unchanged"
    prepared = PreparedTerminalEgressV1(
        target_paths=(), lowering_digest=digest, item_paths=(("k0", kept.path),), unchanged=(kept,)
    )
    assert prepared.changes_nothing
    with pytest.raises(ValidationError):
        PreparedTerminalEgressV1(target_paths=(), lowering_digest=digest, item_paths=())
    # A preparation that changed members keeps its old bytes.
    assert "unchanged" not in PreparedTerminalEgressV1(
        target_paths=(written.path,), lowering_digest=digest, item_paths=(("k1", written.path),)
    ).model_dump(mode="json")
