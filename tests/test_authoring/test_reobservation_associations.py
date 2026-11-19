"""Re-observation is a no-op only when the citation association is already held (F-003, F-012).

The same statement, citation role and origin, under the same verified Capture
contract and observed content, changes no Claim; its admissible re-observation
is recorded separately. A new association on that content -- another role or
origin, or another contract observing the same bytes -- is kept, in either
direction, whether its Capture is the same or another of the same content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.test_authoring.test_authoring_existing_capture import _activate, shared_capture_world
from tests.test_authoring.test_authoring_preflight import _working_payload


def _preview(coordinator: Any, actor: Any, payload: Any, at: str) -> Any:
    _intent, computed = coordinator.preview(actor=actor, payload=payload, canonical_timestamp=at)
    assert computed.lowered is not None, [
        item.model_dump() for item in computed.result.frontier.diagnostics
    ]
    return computed.lowered


def test_the_same_association_on_the_same_content_changes_nothing(tmp_path: Path) -> None:
    _instance, _owner, actor, first_id, _second, coordinator, _other = shared_capture_world(
        tmp_path
    )
    same = _working_payload(occurrence_count=1).model_copy(update={"revises": first_id})
    lowered = _preview(coordinator, actor, same, "2026-08-21T12:04:00.000000Z")
    assert lowered.changed_members == ()
    assert lowered.resolved_authoring["outcome"] == "cruxible.authoring.content_already_cited"


def test_a_new_role_on_already_cited_content_is_kept_in_either_direction(tmp_path: Path) -> None:
    instance, _owner, actor, first_id, _second, coordinator, _other = shared_capture_world(tmp_path)
    as_copy = _working_payload(occurrence_count=1).model_copy(
        update={"revises": first_id, "citation_role": "copy"}
    )
    lowered = _preview(coordinator, actor, as_copy, "2026-08-21T12:04:00.000000Z")
    assert lowered.changed_members, lowered.resolved_authoring
    # Accept the copy association, then cite the content once more as evidence
    # through yet another Capture: evidence is already held, so that is a no-op,
    # while copy, already held too, is also a no-op.
    intent = coordinator.create(
        actor=actor, payload=as_copy, canonical_timestamp="2026-08-21T12:05:00.000000Z"
    ).intent
    _activate(instance, coordinator.submit(intent.intent_id, actor=actor))
    instance.refresh()
    for role in ("evidence", "copy"):
        again = _working_payload(occurrence_count=1).model_copy(
            update={"revises": first_id, "citation_role": role}
        )
        assert (
            _preview(coordinator, actor, again, "2026-08-21T12:06:00.000000Z").changed_members == ()
        )


def test_evidence_on_content_cited_only_as_copy_is_kept(tmp_path: Path) -> None:
    from cruxible_client.contracts.claims import claim_path
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator

    instance, _owner, actor, _first, _second, shared, _other = shared_capture_world(tmp_path)
    coordinator = AuthoringIntentCoordinator(
        instance=instance, store=shared.store, claim_id_factory=lambda: "CLM-" + "5" * 32
    )
    base = _working_payload(occurrence_count=1)
    copy_only = base.model_copy(
        update={
            "statement": base.statement.model_copy(update={"qualifier": "copied"}),
            "citation_role": "copy",
        }
    )
    intent = coordinator.create(
        actor=actor, payload=copy_only, canonical_timestamp="2026-08-21T12:05:00.000000Z"
    ).intent
    _activate(instance, coordinator.submit(intent.intent_id, actor=actor))
    instance.refresh()
    assert claim_path(intent.semantic_identity) in instance.tree_at(
        instance.accepted_coordinate().git_oid
    )
    as_evidence = copy_only.model_copy(
        update={"revises": intent.semantic_identity, "citation_role": "evidence"}
    )
    lowered = _preview(coordinator, actor, as_evidence, "2026-08-21T12:06:00.000000Z")
    assert lowered.changed_members, lowered.resolved_authoring


def _copied(instance: Any, coordinator: Any, actor: Any, first_id: str) -> Any:
    copy = _working_payload(occurrence_count=1).model_copy(
        update={"revises": first_id, "citation_role": "copy"}
    )
    intent = coordinator.create(
        actor=actor, payload=copy, canonical_timestamp="2026-08-21T12:04:00.000000Z"
    ).intent
    _activate(instance, coordinator.submit(intent.intent_id, actor=actor))
    # The same bytes read through another working source: another Capture contract.
    return copy.model_copy(
        update={"source": copy.source.model_copy(update={"source_id": "other.same-bytes"})}
    )


def test_the_same_content_under_another_contract_is_a_new_association(tmp_path: Path) -> None:
    """Review F-012 probe: preview keeps the association and its contract."""

    instance, _owner, actor, first_id, _second, coordinator, _other = shared_capture_world(tmp_path)
    different = _copied(instance, coordinator, actor, first_id)
    lowered = _preview(coordinator, actor, different, "2026-08-21T12:05:00.000000Z")
    assert lowered.changed_members, lowered.resolved_authoring
    assert "outcome" not in lowered.resolved_authoring


def test_submit_agrees_with_preflight_on_another_contract(tmp_path: Path) -> None:
    """Review F-012 probe: submit proposes the revision instead of refusing to record."""

    instance, _owner, actor, first_id, _second, coordinator, _other = shared_capture_world(tmp_path)
    different = _copied(instance, coordinator, actor, first_id)
    intent = coordinator.create(
        actor=actor, payload=different, canonical_timestamp="2026-08-21T12:05:00.000000Z"
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None, submitted
