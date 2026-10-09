"""Re-observation is a no-op only when the citation association is already held (F-003).

The same canonical statement on content the Claim already cites is a no-op only
when the citation role and origin match too; a new association on that content
is kept, in either direction, whether its Capture is the same or another of the
same content.
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
