"""Tests for the ``distinct_from_creation_actor`` mutation guard condition.

The separation anchor is the target entity's committed CREATION receipt — never
a writable property or the last-writer metadata stamp, both of which an agent
could rewrite to launder self-approval. Everything short of positive proof that
the acting actor differs from the creation actor refuses the transition.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.compact import CompactExpansionError, expand_compact
from cruxible_core.config.schema import ActorIdentityGuardCondition
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.service import (
    BatchDirectWriteInput,
    EntityWriteInput,
    service_add_entity_inputs,
    service_batch_direct_write,
)
from cruxible_core.temporal import utc_now

SEPARATION_GUARD_YAML = """\
version: "1.0"
name: actor_separation_state

enums:
  review_status:
    values: [pending, approved]

entity_types:
  Review:
    properties:
      review_id:
        type: string
        primary_key: true
      status:
        type: string
        enum_ref: review_status
      note:
        type: string
        optional: true

mutation_guards:
  - name: review_approval_requires_distinct_authorized_actor
    entity_type: Review
    property: status
    new_value: approved
    condition:
      type: actor
      allowed_actor_ids: [alice, bob]
      distinct_from_creation_actor: true
    message: "Review approvals require an authorized actor distinct from the review's creator."
"""


def _instance(tmp_path: Path) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(dedent(SEPARATION_GUARD_YAML))
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _actor(actor_id: str) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_1",
        operation_id=f"op_{actor_id}",
        timestamp=utc_now(),
    )


def _review_input(status: str, **extra: str) -> EntityWriteInput:
    return EntityWriteInput(
        entity_type="Review",
        entity_id="rev-1",
        properties={"review_id": "rev-1", "status": status, **extra},
    )


def _create_pending_review(
    instance: CruxibleInstance,
    actor: GovernedActorContext | None,
    *,
    create_receipt: bool = True,
) -> None:
    service_add_entity_inputs(
        instance,
        [_review_input("pending")],
        actor_context=actor,
        _create_receipt=create_receipt,
    )


def _approve(instance: CruxibleInstance, actor: GovernedActorContext | None) -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="Review",
                entity_id="rev-1",
                properties={"status": "approved"},
            )
        ],
        actor_context=actor,
    )


def _assert_still_pending(instance: CruxibleInstance) -> None:
    entity = instance.load_graph().get_entity("Review", "rev-1")
    assert entity is not None
    assert entity.properties["status"] == "pending"


class TestDistinctFromCreationActor:
    def test_distinct_authorized_actor_approves(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"))

        _approve(instance, _actor("bob"))

        entity = instance.load_graph().get_entity("Review", "rev-1")
        assert entity is not None
        assert entity.properties["status"] == "approved"

    def test_creator_self_approval_refused(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"))

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_distinct_authorized_actor",
        ):
            _approve(instance, _actor("alice"))
        _assert_still_pending(instance)

    def test_create_with_approved_refused(self, tmp_path: Path) -> None:
        """Creator == actor trivially on create-with-guarded-value: always refused."""
        instance = _instance(tmp_path)

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_distinct_authorized_actor",
        ):
            service_add_entity_inputs(
                instance,
                [_review_input("approved")],
                actor_context=_actor("bob"),
            )
        assert instance.load_graph().get_entity("Review", "rev-1") is None

    def test_creation_without_recorded_actor_refused(self, tmp_path: Path) -> None:
        """Pre-auth creation receipts carry no actor: separation cannot pass."""
        instance = _instance(tmp_path)
        _create_pending_review(instance, actor=None)

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_distinct_authorized_actor",
        ):
            _approve(instance, _actor("bob"))
        _assert_still_pending(instance)

    def test_missing_creation_receipt_refused(self, tmp_path: Path) -> None:
        """No committed creation receipt (e.g. clone/import records): refused."""
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"), create_receipt=False)

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_distinct_authorized_actor",
        ):
            _approve(instance, _actor("bob"))
        _assert_still_pending(instance)

    def test_provenance_lookup_error_refused(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"))

        with (
            patch.object(
                instance,
                "get_receipt_store",
                side_effect=RuntimeError("receipt store unavailable"),
            ),
            pytest.raises(
                DataValidationError,
                match="review_approval_requires_distinct_authorized_actor",
            ),
        ):
            _approve(instance, _actor("bob"))
        _assert_still_pending(instance)

    def test_allow_list_still_enforced_for_distinct_actor(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"))

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_distinct_authorized_actor",
        ):
            _approve(instance, _actor("mallory"))
        _assert_still_pending(instance)

    def test_missing_actor_context_refused(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"))

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_distinct_authorized_actor",
        ):
            _approve(instance, actor=None)
        _assert_still_pending(instance)

    def test_later_update_does_not_launder_creation_actor(self, tmp_path: Path) -> None:
        """A non-creator touching the entity must not free the creator to approve.

        The last-writer metadata stamp moves to bob on update; the creation
        receipt still names alice, and the creation receipt is the anchor.
        """
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"))
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-1",
                    properties={"note": "touched by bob"},
                )
            ],
            actor_context=_actor("bob"),
        )

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_distinct_authorized_actor",
        ):
            _approve(instance, _actor("alice"))
        _assert_still_pending(instance)

        _approve(instance, _actor("bob"))
        entity = instance.load_graph().get_entity("Review", "rev-1")
        assert entity is not None
        assert entity.properties["status"] == "approved"

    def test_spoofed_metadata_actor_context_does_not_launder(self, tmp_path: Path) -> None:
        """Caller-supplied EntityMetadata.actor_context is writable and must not count."""
        instance = _instance(tmp_path)
        spoofed_bob = {
            "actor_context": {
                "actor_type": "human_user",
                "actor_id": "bob",
                "org_id": "org_1",
                "operation_id": "op_spoof",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        }
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-1",
                    properties={"review_id": "rev-1", "status": "pending"},
                    metadata=spoofed_bob,
                )
            ],
            actor_context=_actor("alice"),
        )

        with pytest.raises(
            DataValidationError,
            match="review_approval_requires_distinct_authorized_actor",
        ):
            _approve(instance, _actor("alice"))
        _assert_still_pending(instance)

    def test_refused_attempt_does_not_poison_provenance(self, tmp_path: Path) -> None:
        """An uncommitted (refused) receipt is ignored; a real approver still passes."""
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"))
        with pytest.raises(DataValidationError):
            _approve(instance, _actor("alice"))

        _approve(instance, _actor("bob"))
        entity = instance.load_graph().get_entity("Review", "rev-1")
        assert entity is not None
        assert entity.properties["status"] == "approved"

    def test_batch_dry_run_reports_self_approval_without_mutating(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _create_pending_review(instance, _actor("alice"))

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-1",
                        properties={"status": "approved"},
                    )
                ]
            ),
            dry_run=True,
            actor_context=_actor("alice"),
        )

        assert result.valid is False
        assert any(
            "review_approval_requires_distinct_authorized_actor" in error
            for error in result.validation_errors
        )
        _assert_still_pending(instance)


class TestConditionSchema:
    def test_defaults_to_false(self) -> None:
        condition = ActorIdentityGuardCondition(type="actor", allowed_actor_ids=["alice"])
        assert condition.distinct_from_creation_actor is False

    def test_accepts_boolean(self) -> None:
        condition = ActorIdentityGuardCondition(
            type="actor",
            allowed_actor_ids=["alice"],
            distinct_from_creation_actor=True,
        )
        assert condition.distinct_from_creation_actor is True

    @pytest.mark.parametrize("value", ["true", "yes", 1, 0, [True], None])
    def test_rejects_non_boolean(self, value: object) -> None:
        with pytest.raises(ValidationError):
            ActorIdentityGuardCondition(
                type="actor",
                allowed_actor_ids=["alice"],
                distinct_from_creation_actor=value,  # type: ignore[arg-type]
            )

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ActorIdentityGuardCondition.model_validate(
                {
                    "type": "actor",
                    "allowed_actor_ids": ["alice"],
                    "distinct_from_creation_author": True,
                }
            )


_COMPACT_GUARD_SOURCE = """\
name: k
entity_types:
  Review:
    id: review_id
    properties:
      status: string
"""


class TestCompactExpansion:
    def test_distinct_key_passes_through(self) -> None:
        config = expand_compact(
            _COMPACT_GUARD_SOURCE
            + dedent(
                """
                mutation_guards:
                  - g:
                      when: Review.status -> approved
                      require: {allowed_actors: [reviewer], distinct_from_creation_actor: true}
                """
            )
        )
        assert config["mutation_guards"][0]["condition"] == {
            "type": "actor",
            "allowed_actor_ids": ["reviewer"],
            "distinct_from_creation_actor": True,
        }

    def test_omitted_key_stays_omitted(self) -> None:
        config = expand_compact(
            _COMPACT_GUARD_SOURCE
            + dedent(
                """
                mutation_guards:
                  - g:
                      when: Review.status -> approved
                      require: {allowed_actors: [reviewer]}
                """
            )
        )
        assert config["mutation_guards"][0]["condition"] == {
            "type": "actor",
            "allowed_actor_ids": ["reviewer"],
        }

    def test_readme_guard_label_mentions_separation(self) -> None:
        from cruxible_core.canonical_views.markdown import render_mutation_guards_markdown
        from cruxible_core.config.loader import load_config_from_string

        config = load_config_from_string(
            _COMPACT_GUARD_SOURCE
            + dedent(
                """
                mutation_guards:
                  - g:
                      when: Review.status -> approved
                      require: {allowed_actors: [reviewer], distinct_from_creation_actor: true}
                """
            )
        )
        rendered = render_mutation_guards_markdown(config)
        assert "authenticated actor in: reviewer" in rendered
        assert "actor differs from the entity's creation actor" in rendered

    def test_unknown_require_key_still_refused(self) -> None:
        with pytest.raises(CompactExpansionError, match="distinct_from_creation_author"):
            expand_compact(
                _COMPACT_GUARD_SOURCE
                + dedent(
                    """
                    mutation_guards:
                      - g:
                          when: Review.status -> approved
                          require: {allowed_actors: [reviewer], distinct_from_creation_author: true}
                    """
                )
            )
