"""Tests for the frozen-property mutation guard condition (``type: frozen``).

The other guard conditions trigger only on transitions TO named values, so no
property could be protected from ANY change. The freeze condition protects a
property outright: updates that change it are refused while the entity's
STORED, pre-write state matches an optional ``while`` clause (no clause =
immutable after create). Creates set the property freely. Everything evaluates
against before-state, so a single write that both leaves the freeze state and
changes the frozen property (demote + retarget) is refused, and an update
whose stored state cannot be read fails closed.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.compact import CompactExpansionError, expand_compact
from cruxible_core.config.schema import CoreConfig, FrozenPropertyGuardCondition
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.operations import ValidatedEntity
from cruxible_core.graph.types import EntityInstance
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    service_add_entity_inputs,
    service_batch_direct_write,
)
from cruxible_core.service.mutation_guards import mutation_guard_errors
from cruxible_core.temporal import utc_now

FREEZE_GUARD_YAML = """\
version: "1.0"
name: property_freeze_state

enums:
  review_status:
    values: [requested, approved, withdrawn]

entity_types:
  Review:
    properties:
      review_id:
        type: string
        primary_key: true
      status:
        type: string
        enum_ref: review_status
      head:
        type: string
        optional: true
      summary:
        type: string
        optional: true
  Note:
    properties:
      note_id:
        type: string
        primary_key: true
      kind:
        type: string
      body:
        type: string
        optional: true

mutation_guards:
  - name: review_head_frozen_while_approved
    entity_type: Review
    property: head
    condition:
      type: frozen
      while: {status: approved}
    message: "approved reviews pin the reviewed head"
  - name: note_kind_immutable
    entity_type: Note
    property: kind
    condition:
      type: frozen
    message: "note kind is fixed at creation"
"""


def _instance(tmp_path: Path) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(dedent(FREEZE_GUARD_YAML))
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _write_review(instance: CruxibleInstance, properties: dict) -> None:
    service_add_entity_inputs(
        instance,
        [EntityWriteInput(entity_type="Review", entity_id="rev-1", properties=properties)],
    )


def _review_property(instance: CruxibleInstance, name: str) -> object:
    entity = instance.load_graph().get_entity("Review", "rev-1")
    assert entity is not None
    return entity.properties.get(name)


def _base_config_dict() -> dict:
    return {
        "version": "1.0",
        "name": "schema_shape",
        "entity_types": {
            "Review": {
                "properties": {
                    "review_id": {"type": "string", "primary_key": True},
                    "status": {"type": "string"},
                    "head": {"type": "string", "optional": True},
                }
            }
        },
    }


def _config_with_guard(guard: dict) -> CoreConfig:
    return CoreConfig.model_validate({**_base_config_dict(), "mutation_guards": [guard]})


class TestFrozenGuardSchema:
    def test_frozen_guard_validates_with_and_without_while(self) -> None:
        config = _config_with_guard(
            {
                "name": "g",
                "entity_type": "Review",
                "property": "head",
                "condition": {"type": "frozen", "while": {"status": "approved"}},
            }
        )
        condition = config.mutation_guards[0].condition
        assert isinstance(condition, FrozenPropertyGuardCondition)
        assert condition.while_state == {"status": "approved"}

        config = _config_with_guard(
            {
                "name": "g",
                "entity_type": "Review",
                "property": "head",
                "condition": {"type": "frozen"},
            }
        )
        condition = config.mutation_guards[0].condition
        assert isinstance(condition, FrozenPropertyGuardCondition)
        assert condition.while_state is None

    def test_unknown_condition_key_refused(self) -> None:
        with pytest.raises(ValidationError):
            _config_with_guard(
                {
                    "name": "g",
                    "entity_type": "Review",
                    "property": "head",
                    "condition": {"type": "frozen", "unless": {"status": "approved"}},
                }
            )

    def test_new_value_refused(self) -> None:
        with pytest.raises(ValidationError, match="new_value is not allowed"):
            _config_with_guard(
                {
                    "name": "g",
                    "entity_type": "Review",
                    "property": "head",
                    "new_value": "sha",
                    "condition": {"type": "frozen"},
                }
            )

    def test_relationship_type_refused(self) -> None:
        with pytest.raises(ValidationError, match="entity types only"):
            _config_with_guard(
                {
                    "name": "g",
                    "entity_type": "Review",
                    "property": "head",
                    "relationship_type": "review_pins_commit",
                    "condition": {"type": "frozen"},
                }
            )

    def test_where_scoping_refused(self) -> None:
        with pytest.raises(ValidationError, match="'while' clause"):
            _config_with_guard(
                {
                    "name": "g",
                    "entity_type": "Review",
                    "property": "head",
                    "where": {"candidate.status": {"eq": "approved"}},
                    "condition": {"type": "frozen"},
                }
            )

    @pytest.mark.parametrize("field", ["where_related", "where_not_related"])
    def test_explicit_empty_related_scoping_accepted(self, field: str) -> None:
        """Empty related-scoping lists are accepted: config composition
        round-trips guards through model_dump, which serializes the default
        empty lists explicitly — refusing presence would refuse the guard's
        own round-trip (caught by composed-init integration tests). An empty
        list declares no scoping, so accepting it weakens nothing; populated
        lists remain refused."""
        config = _config_with_guard(
            {
                "name": "g",
                "entity_type": "Review",
                "property": "head",
                field: [],
                "condition": {"type": "frozen"},
            }
        )
        guard = config.mutation_guards[0]
        # Pin the exact round-trip the composer performs (composer.py:177):
        # exclude_none drops the None-valued fields whose presence would trip
        # their own checks (new_value), while default empty lists survive the
        # dump — which is why they must be accepted, not presence-refused.
        dumped = guard.model_dump(mode="python", by_alias=True, exclude_none=True)
        assert dumped[field] == []
        type(guard).model_validate(dumped)

    def test_empty_while_refused(self) -> None:
        with pytest.raises(ValidationError, match="at least one"):
            _config_with_guard(
                {
                    "name": "g",
                    "entity_type": "Review",
                    "property": "head",
                    "condition": {"type": "frozen", "while": {}},
                }
            )

    def test_missing_property_refused(self) -> None:
        with pytest.raises(ValidationError, match="frozen property guards require"):
            _config_with_guard(
                {
                    "name": "g",
                    "entity_type": "Review",
                    "condition": {"type": "frozen"},
                }
            )


class TestFrozenGuardLint:
    def _validate(self, guard: dict) -> None:
        validate_config(_config_with_guard(guard))

    def test_valid_guard_passes(self) -> None:
        self._validate(
            {
                "name": "g",
                "entity_type": "Review",
                "property": "head",
                "condition": {"type": "frozen", "while": {"status": "approved"}},
            }
        )

    def test_frozen_property_must_exist(self) -> None:
        with pytest.raises(ConfigError, match="frozen property 'sha' not found"):
            self._validate(
                {
                    "name": "g",
                    "entity_type": "Review",
                    "property": "sha",
                    "condition": {"type": "frozen"},
                }
            )

    def test_while_property_must_exist(self) -> None:
        with pytest.raises(ConfigError, match="'while' property 'state' not found"):
            self._validate(
                {
                    "name": "g",
                    "entity_type": "Review",
                    "property": "head",
                    "condition": {"type": "frozen", "while": {"state": "approved"}},
                }
            )

    def test_unknown_entity_type_refused(self) -> None:
        with pytest.raises(ConfigError, match="not defined in entity_types"):
            self._validate(
                {
                    "name": "g",
                    "entity_type": "Ghost",
                    "property": "head",
                    "condition": {"type": "frozen"},
                }
            )

    def test_relationship_type_named_as_entity_type_refused(self) -> None:
        """v1 scope is entity types only: a freeze naming a relationship type is refused."""
        config_dict = {
            **_base_config_dict(),
            "entity_types": {
                **_base_config_dict()["entity_types"],
                "Commit": {"properties": {"commit_id": {"type": "string", "primary_key": True}}},
            },
            "relationships": [
                {
                    "name": "review_pins_commit",
                    "from_entity": "Review",
                    "to_entity": "Commit",
                    "properties": {"pinned_head": {"type": "string"}},
                }
            ],
            "mutation_guards": [
                {
                    "name": "g",
                    "entity_type": "review_pins_commit",
                    "property": "pinned_head",
                    "condition": {"type": "frozen"},
                }
            ],
        }
        with pytest.raises(
            ConfigError,
            match="is a relationship type; frozen property guards support entity types only",
        ):
            validate_config(CoreConfig.model_validate(config_dict))

    def test_while_value_must_normalize(self) -> None:
        config_dict = _base_config_dict()
        config_dict["enums"] = {"review_status": {"values": ["requested", "approved"]}}
        config_dict["entity_types"]["Review"]["properties"]["status"] = {
            "type": "string",
            "enum_ref": "review_status",
        }
        config_dict["mutation_guards"] = [
            {
                "name": "g",
                "entity_type": "Review",
                "property": "head",
                "condition": {"type": "frozen", "while": {"status": "not-a-status"}},
            }
        ]
        with pytest.raises(ConfigError, match="'while' value for property 'status'"):
            validate_config(CoreConfig.model_validate(config_dict))


class TestFrozenGuardCompact:
    COMPACT_BASE = """\
    version: "1.0"
    name: t
    metadata: {}
    entity_types:
      Review:
        id: review_id
        properties:
          status: string
          head: string?
    """

    def _expand(self, guards_yaml: str) -> dict:
        return expand_compact(dedent(self.COMPACT_BASE) + dedent(guards_yaml))

    def test_freeze_trigger_expands(self) -> None:
        expanded = self._expand(
            """\
            mutation_guards:
              - head_frozen:
                  freeze: Review.head
                  while: {status: approved}
                  message: pinned
            """
        )
        assert expanded["mutation_guards"] == [
            {
                "name": "head_frozen",
                "entity_type": "Review",
                "property": "head",
                "condition": {"type": "frozen", "while": {"status": "approved"}},
                "message": "pinned",
            }
        ]

    def test_freeze_without_while_expands_unconditional(self) -> None:
        expanded = self._expand(
            """\
            mutation_guards:
              - head_frozen: {freeze: Review.head}
            """
        )
        assert expanded["mutation_guards"][0]["condition"] == {"type": "frozen"}

    def test_freeze_alongside_require_refused(self) -> None:
        with pytest.raises(CompactExpansionError, match="unsupported key 'require'"):
            self._expand(
                """\
                mutation_guards:
                  - head_frozen:
                      freeze: Review.head
                      require: {allowed_actors: [reviewer]}
                """
            )

    def test_malformed_freeze_trigger_refused(self) -> None:
        with pytest.raises(CompactExpansionError, match="freeze must be"):
            self._expand(
                """\
                mutation_guards:
                  - head_frozen: {freeze: 'Review.head -> sha'}
                """
            )

    def test_non_mapping_while_refused(self) -> None:
        with pytest.raises(CompactExpansionError, match="while must be a property=value mapping"):
            self._expand(
                """\
                mutation_guards:
                  - head_frozen:
                      freeze: Review.head
                      while: approved
                """
            )

    def test_while_on_when_guard_refused(self) -> None:
        """The while clause belongs to the freeze form; when-guards refuse it fail-closed."""
        with pytest.raises(CompactExpansionError, match="unsupported key 'while'"):
            self._expand(
                """\
                mutation_guards:
                  - g:
                      when: Review.status -> approved
                      require: {allowed_actors: [reviewer]}
                      while: {status: approved}
                """
            )


class TestConditionalFreezeEnforcement:
    """``while`` clause semantics against the stored, pre-write state."""

    def test_change_refused_while_state_matches(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})

        with pytest.raises(DataValidationError, match="review_head_frozen_while_approved"):
            _write_review(instance, {"head": "sha-2"})
        assert _review_property(instance, "head") == "sha-1"

    def test_change_allowed_while_state_does_not_match(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "requested", "head": "sha-1"})

        _write_review(instance, {"head": "sha-2"})
        assert _review_property(instance, "head") == "sha-2"

    def test_missing_while_property_does_not_match(self) -> None:
        """A stored entity without the clause property is not in the freeze state."""
        config = CoreConfig.model_validate(
            {
                **_base_config_dict(),
                "mutation_guards": [
                    {
                        "name": "head_frozen",
                        "entity_type": "Review",
                        "property": "head",
                        "condition": {"type": "frozen", "while": {"status": "approved"}},
                    }
                ],
            }
        )
        stored = EntityInstance(
            entity_type="Review",
            entity_id="rev-1",
            # No stored `status` at all: the clause names status=approved, and
            # an absent stored property matches no named value.
            properties={"review_id": "rev-1", "head": "sha-1"},
        )
        proposed = EntityInstance(
            entity_type="Review",
            entity_id="rev-1",
            properties={"review_id": "rev-1", "head": "sha-2"},
        )
        current_graph = EntityGraph()
        current_graph.add_entity(stored)
        proposed_graph = EntityGraph()
        proposed_graph.add_entity(proposed)
        errors = mutation_guard_errors(
            config,
            current_graph=current_graph,
            proposed_graph=proposed_graph,
            entities=[ValidatedEntity(entity=proposed, is_update=True)],
        )
        assert errors == []

    def test_reasserting_stored_value_is_not_a_change(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})

        _write_review(instance, {"head": "sha-1"})
        assert _review_property(instance, "head") == "sha-1"

    def test_set_from_unset_refused_while_state_matches(self, tmp_path: Path) -> None:
        """Setting a previously-unset frozen property is a change like any other."""
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved"})

        with pytest.raises(DataValidationError, match="review_head_frozen_while_approved"):
            _write_review(instance, {"head": "sha-1"})
        assert _review_property(instance, "head") is None

    def test_demote_and_retarget_in_one_write_refused(self, tmp_path: Path) -> None:
        """Before-state semantics: leaving the freeze state in the same write does not help."""
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})

        with pytest.raises(DataValidationError, match="review_head_frozen_while_approved"):
            _write_review(instance, {"status": "withdrawn", "head": "sha-2"})
        assert _review_property(instance, "status") == "approved"
        assert _review_property(instance, "head") == "sha-1"

    def test_demote_then_retarget_in_separate_writes_allowed(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})

        _write_review(instance, {"status": "withdrawn"})
        _write_review(instance, {"head": "sha-2"})
        assert _review_property(instance, "head") == "sha-2"

    def test_create_sets_frozen_property_freely(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})
        assert _review_property(instance, "head") == "sha-1"

    def test_other_properties_stay_writable_while_frozen(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})

        _write_review(instance, {"summary": "still writable"})
        assert _review_property(instance, "summary") == "still writable"
        assert _review_property(instance, "head") == "sha-1"


class TestUnconditionalFreezeEnforcement:
    def test_update_refused(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Note",
                    entity_id="n-1",
                    properties={"note_id": "n-1", "kind": "review_note"},
                )
            ],
        )

        with pytest.raises(DataValidationError, match="note_kind_immutable"):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="Note",
                        entity_id="n-1",
                        properties={"kind": "scratchpad"},
                    )
                ],
            )
        entity = instance.load_graph().get_entity("Note", "n-1")
        assert entity is not None
        assert entity.properties["kind"] == "review_note"

    def test_create_and_other_property_updates_allowed(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Note",
                    entity_id="n-1",
                    properties={"note_id": "n-1", "kind": "scratchpad"},
                )
            ],
        )
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Note",
                    entity_id="n-1",
                    properties={"body": "updated body", "kind": "scratchpad"},
                )
            ],
        )
        entity = instance.load_graph().get_entity("Note", "n-1")
        assert entity is not None
        assert entity.properties["body"] == "updated body"


class TestBatchDirectWriteFreezeEnforcement:
    def test_batch_update_refused_atomically(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})

        with pytest.raises(DataValidationError, match="review_head_frozen_while_approved"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="Review",
                            entity_id="rev-1",
                            properties={"head": "sha-2"},
                        ),
                        EntityWriteInput(
                            entity_type="Note",
                            entity_id="n-batch",
                            properties={"note_id": "n-batch", "kind": "field_note"},
                        ),
                    ],
                ),
            )
        assert _review_property(instance, "head") == "sha-1"
        assert instance.load_graph().get_entity("Note", "n-batch") is None

    @pytest.mark.parametrize("demote_first", [True, False])
    def test_demote_and_retarget_as_duplicate_batch_entries_refused(
        self, tmp_path: Path, demote_first: bool
    ) -> None:
        """The freeze attack split across two batch entries for the SAME entity
        (demote status in one entry, retarget head in the other, either order)
        is refused by the batch's generic duplicate-entity rejection before any
        write applies."""
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})

        demote = EntityWriteInput(
            entity_type="Review",
            entity_id="rev-1",
            properties={"status": "withdrawn"},
        )
        retarget = EntityWriteInput(
            entity_type="Review",
            entity_id="rev-1",
            properties={"head": "sha-2"},
        )
        entries = [demote, retarget] if demote_first else [retarget, demote]
        with pytest.raises(DataValidationError, match="duplicate in batch"):
            service_batch_direct_write(instance, BatchDirectWriteInput(entities=entries))
        assert _review_property(instance, "status") == "approved"
        assert _review_property(instance, "head") == "sha-1"

    def test_batch_dry_run_reports_freeze_refusal(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-1"})

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-1",
                        properties={"head": "sha-2"},
                    )
                ],
            ),
            dry_run=True,
        )
        assert not result.valid
        assert any(
            "review_head_frozen_while_approved" in error for error in result.validation_errors
        )
        assert _review_property(instance, "head") == "sha-1"


class TestFrozenGuardFailClosed:
    def test_update_with_unreadable_stored_state_refused(self) -> None:
        """An update whose stored pre-write entity cannot be read is refused."""
        config = CoreConfig.model_validate(
            {
                **_base_config_dict(),
                "mutation_guards": [
                    {
                        "name": "head_frozen",
                        "entity_type": "Review",
                        "property": "head",
                        "condition": {"type": "frozen", "while": {"status": "approved"}},
                    }
                ],
            }
        )
        entity = EntityInstance(
            entity_type="Review",
            entity_id="rev-1",
            properties={"review_id": "rev-1", "status": "requested", "head": "sha-2"},
        )
        proposed_graph = EntityGraph()
        proposed_graph.add_entity(entity)
        errors = mutation_guard_errors(
            config,
            # The current graph has no stored record for this update: the
            # before-state is unreadable, so the freeze refuses.
            current_graph=EntityGraph(),
            proposed_graph=proposed_graph,
            entities=[ValidatedEntity(entity=entity, is_update=True)],
        )
        assert len(errors) == 1
        assert "head_frozen" in errors[0]
        assert "fail-closed" in errors[0]

    @staticmethod
    def _stale_clause_config() -> CoreConfig:
        """A frozen guard whose ``while`` clause value cannot normalize.

        The clause names ``status: approved`` but the enum no longer contains
        ``approved`` — a stale/programmatically-constructed config that skipped
        lint. ``normalize_value`` raises for the clause value.
        """
        return CoreConfig.model_validate(
            {
                "version": "1.0",
                "name": "stale_clause",
                "enums": {"review_status": {"values": ["requested", "withdrawn"]}},
                "entity_types": {
                    "Review": {
                        "properties": {
                            "review_id": {"type": "string", "primary_key": True},
                            "status": {"type": "string", "enum_ref": "review_status"},
                            "head": {"type": "string", "optional": True},
                        }
                    }
                },
                "mutation_guards": [
                    {
                        "name": "head_frozen",
                        "entity_type": "Review",
                        "property": "head",
                        "condition": {"type": "frozen", "while": {"status": "approved"}},
                    }
                ],
            }
        )

    @staticmethod
    def _frozen_update_errors(config: CoreConfig, stored_properties: dict) -> list[str]:
        stored = EntityInstance(
            entity_type="Review",
            entity_id="rev-1",
            properties=stored_properties,
        )
        proposed = EntityInstance(
            entity_type="Review",
            entity_id="rev-1",
            properties={**stored_properties, "head": "sha-2"},
        )
        current_graph = EntityGraph()
        current_graph.add_entity(stored)
        proposed_graph = EntityGraph()
        proposed_graph.add_entity(proposed)
        return mutation_guard_errors(
            config,
            current_graph=current_graph,
            proposed_graph=proposed_graph,
            entities=[ValidatedEntity(entity=proposed, is_update=True)],
        )

    def test_unnormalizable_while_clause_refuses_mutation(self) -> None:
        """Normalization failure on a frozen guard refuses the write outright.

        Regression for the fail-open fallback: the evaluator used to compare
        the raw authored clause value when normalization raised, so a stored
        value differing from the raw spelling silently deactivated the freeze.
        """
        errors = self._frozen_update_errors(
            self._stale_clause_config(),
            {"review_id": "rev-1", "status": "requested", "head": "sha-1"},
        )
        assert len(errors) == 1
        assert "head_frozen" in errors[0]
        assert "status='approved'" in errors[0]
        assert "does not normalize" in errors[0]
        assert "fail-closed" in errors[0]

    def test_unnormalizable_while_clause_refuses_even_without_stored_property(self) -> None:
        """An unnormalizable clause refuses regardless of stored state — the
        stored-property-missing short-circuit does not skip clause validation."""
        errors = self._frozen_update_errors(
            self._stale_clause_config(),
            {"review_id": "rev-1", "head": "sha-1"},
        )
        assert len(errors) == 1
        assert "status='approved'" in errors[0]
        assert "fail-closed" in errors[0]


KIT_CONFIG = Path(__file__).resolve().parents[2] / "kits" / "agent-operation" / "config.yaml"


def _kit_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(KIT_CONFIG.read_text())
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _kit_actor(actor_id: str) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_1",
        operation_id=f"op_{actor_id}",
        timestamp=utc_now(),
    )


def _seed_review(instance: CruxibleInstance, *, approve: bool) -> None:
    """Create rr-1 (change_head pinned) as implementer; optionally approve as reviewer.

    Approval satisfies the kit's verdict guards: the authorized-reviewer actor
    (distinct from the creator) co-writes the required review_note in the same
    batch.
    """
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="ReviewRequest",
                entity_id="rr-1",
                properties={
                    "review_request_id": "rr-1",
                    "title": "Freeze the head",
                    "status": "requested",
                    "change_head": "sha-reviewed",
                },
            )
        ],
        actor_context=_kit_actor("impl-agent"),
    )
    if not approve:
        return
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-1",
                    properties={"status": "approved"},
                ),
                _note("sn-approve", kind="review_note"),
            ],
            relationships=[_note_edge("sn-approve")],
        ),
        actor_context=_kit_actor("authorized-reviewer"),
    )


def _note(note_id: str, *, kind: str) -> EntityWriteInput:
    return EntityWriteInput(
        entity_type="StateNote",
        entity_id=note_id,
        properties={
            "note_id": note_id,
            "kind": kind,
            "title": "Note",
            "summary": "Summary.",
            "body": "Body.",
            "created_at": utc_now(),
        },
    )


def _note_edge(note_id: str) -> BatchRelationshipWriteInput:
    return BatchRelationshipWriteInput(
        from_type="StateNote",
        from_id=note_id,
        relationship_type="state_note_about_review_request",
        to_type="ReviewRequest",
        to_id="rr-1",
    )


def _kit_change_head(instance: CruxibleInstance) -> object:
    entity = instance.load_graph().get_entity("ReviewRequest", "rr-1")
    assert entity is not None
    return entity.properties.get("change_head")


class TestAgentOperationFreezeDeclarations:
    """The two shipped kit declarations behave as intended."""

    def test_approved_review_change_head_retarget_refused(self, tmp_path: Path) -> None:
        instance = _kit_instance(tmp_path)
        _seed_review(instance, approve=True)

        with pytest.raises(
            DataValidationError,
            match="review_request_change_head_frozen_after_approval",
        ):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="ReviewRequest",
                        entity_id="rr-1",
                        properties={"change_head": "sha-unreviewed"},
                    )
                ],
                actor_context=_kit_actor("impl-agent"),
            )
        assert _kit_change_head(instance) == "sha-reviewed"

    def test_withdraw_and_retarget_in_one_write_refused(self, tmp_path: Path) -> None:
        """The merge-gate bypass: demote the review and move its pin in one write."""
        instance = _kit_instance(tmp_path)
        _seed_review(instance, approve=True)

        with pytest.raises(
            DataValidationError,
            match="review_request_change_head_frozen_after_approval",
        ):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="ReviewRequest",
                            entity_id="rr-1",
                            properties={
                                "status": "withdrawn",
                                "change_head": "sha-unreviewed",
                            },
                        ),
                        _note("sn-withdraw", kind="review_note"),
                    ],
                    relationships=[_note_edge("sn-withdraw")],
                ),
                actor_context=_kit_actor("authorized-reviewer"),
            )
        entity = instance.load_graph().get_entity("ReviewRequest", "rr-1")
        assert entity is not None
        assert entity.properties["status"] == "approved"
        assert entity.properties["change_head"] == "sha-reviewed"

    def test_requested_review_change_head_still_writable(self, tmp_path: Path) -> None:
        instance = _kit_instance(tmp_path)
        _seed_review(instance, approve=False)

        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-1",
                    properties={"change_head": "sha-rebased"},
                )
            ],
            actor_context=_kit_actor("impl-agent"),
        )
        assert _kit_change_head(instance) == "sha-rebased"

    def test_state_note_rekind_to_scratchpad_refused(self, tmp_path: Path) -> None:
        """The curated-read hiding hole: a rationale note demoted to scratchpad."""
        instance = _kit_instance(tmp_path)
        _seed_review(instance, approve=True)

        with pytest.raises(DataValidationError, match="state_note_kind_immutable"):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="StateNote",
                        entity_id="sn-approve",
                        properties={"kind": "scratchpad"},
                    )
                ],
                actor_context=_kit_actor("impl-agent"),
            )
        entity = instance.load_graph().get_entity("StateNote", "sn-approve")
        assert entity is not None
        assert entity.properties["kind"] == "review_note"

    def test_state_note_content_update_still_allowed(self, tmp_path: Path) -> None:
        instance = _kit_instance(tmp_path)
        _seed_review(instance, approve=True)

        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="StateNote",
                    entity_id="sn-approve",
                    properties={"body": "Amended body."},
                )
            ],
            actor_context=_kit_actor("impl-agent"),
        )
        entity = instance.load_graph().get_entity("StateNote", "sn-approve")
        assert entity is not None
        assert entity.properties["body"] == "Amended body."
        assert entity.properties["kind"] == "review_note"
