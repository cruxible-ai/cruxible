"""Receipted procedure lifecycle, attribution, and digest-pinning tests."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.schema import ContractSchema
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.types import (
    ProcedureContractSchema,
    ProcedureDefinition,
    ProcedureRecord,
    compute_procedure_definition_digest,
)
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    service_accept_procedure,
    service_get_procedure,
    service_get_procedure_details,
    service_lock,
    service_propose_procedure,
    service_reject_procedure,
    service_retire_procedure,
)
from cruxible_core.workflow.compiler import (
    compute_lock_config_digest,
    compute_lock_digest,
    load_lock,
    resolve_lock_path,
)
from cruxible_core.workflow.contracts import validate_contract_payload
from tests.test_procedures.conftest import actor, provider_definition


def _receipt(instance: CruxibleInstance, receipt_id: str) -> Receipt:
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
        assert receipt is not None
        return receipt
    finally:
        store.close()


def _contract_in_schema(
    instance: CruxibleInstance,
    name: str,
    contract_in: Mapping[str, Any],
) -> ProcedureContractSchema:
    """Propose a procedure carrying ``contract_in`` and return its discovery schema."""
    fields = contract_in["fields"]
    assert isinstance(fields, dict)
    definition = ProcedureDefinition.model_validate(
        {
            "name": name,
            "contract_in": contract_in,
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {field_name: f"$input.{field_name}" for field_name in sorted(fields)},
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )
    proposed = service_propose_procedure(
        instance,
        definition,
        actor_context=actor("proposer"),
    )
    schema = service_get_procedure_details(
        instance,
        proposed.procedure.procedure_id,
    ).contract_in_schema
    assert schema is not None
    return schema


def _assert_example_satisfies_contract(
    instance: CruxibleInstance,
    contract_in: Mapping[str, Any],
    example: Mapping[str, Any] | None,
) -> None:
    """Assert a generated example passes the very contract it demonstrates."""
    assert example is not None
    validate_contract_payload(
        instance.load_config(),
        ContractSchema.model_validate(contract_in),
        dict(example),
        subject="Procedure input example",
        error_factory=ConfigError,
    )


def test_propose_and_accept_pin_definition_config_and_lock_digests(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition()
    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )

    assert proposed.procedure.status == "pending"
    assert proposed.procedure.version == 1
    assert proposed.procedure.definition_digest == compute_procedure_definition_digest(definition)
    assert proposed.receipt_id is not None
    proposal_receipt = _receipt(procedure_instance, proposed.receipt_id)
    assert proposal_receipt.operation_type == "procedure_transition"
    assert proposal_receipt.committed is True

    accepted = service_accept_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer"),
    )

    config = procedure_instance.load_config()
    lock = load_lock(resolve_lock_path(procedure_instance))
    assert accepted.procedure.status == "live"
    assert accepted.procedure.version == 2
    assert accepted.procedure.definition_digest == proposed.procedure.definition_digest
    assert accepted.procedure.acceptance_config_digest == compute_lock_config_digest(config)
    assert accepted.procedure.acceptance_lock_digest == compute_lock_digest(lock)
    assert accepted.procedure.resolved_actor_context == actor("reviewer")
    assert accepted.receipt_id is not None
    assert _receipt(procedure_instance, accepted.receipt_id).committed is True

    details = service_get_procedure_details(
        procedure_instance,
        proposed.procedure.procedure_id,
    )
    assert details.procedure == accepted.procedure
    assert details.contract_in_schema is not None
    assert details.contract_in_schema.model_dump(exclude_none=True) == {
        "fields": [{"name": "value", "type": "int", "required": True}],
        "allow_extra": False,
        # The worked payload the field list would otherwise leave the caller to
        # invent -- every key they must supply, with a value of the right type.
        "input_example": {"value": 1},
    }


def test_proposal_refuses_unknown_precondition_entity_type_with_receipt(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition(
        "unknown_precondition_type",
        precondition={
            "entity_type": "UnknownType",
            "condition": {"status": "ready"},
        },
    )

    with pytest.raises(
        ConfigError,
        match="precondition references unknown entity type 'UnknownType'",
    ) as exc_info:
        service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("proposer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    receipt = _receipt(procedure_instance, exc_info.value.mutation_receipt_id)
    assert receipt.committed is False
    assert any(
        "UnknownType" in str(node.detail.get("reason", ""))
        for node in receipt.nodes
        if node.node_type == "validation"
    )


def test_proposal_blocks_input_reference_missing_from_contract(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition("invalid_contract_reference")
    definition = definition.model_copy(
        update={
            "steps": [
                definition.steps[0].model_copy(
                    update={"input": {"value": "$input.transactions_arguments"}}
                )
            ]
        }
    )

    with pytest.raises(ConfigError) as exc_info:
        service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("proposer"),
        )

    message = str(exc_info.value)
    assert "step 'invoke'" in message
    assert "'$input.transactions_arguments'" in message
    assert "value (int, required)" in message
    store = procedure_instance.get_procedure_store()
    try:
        assert store.count_procedures() == 0
    finally:
        store.close()


def test_proposal_returns_non_blocking_authoring_warnings(
    procedure_instance: CruxibleInstance,
) -> None:
    config = procedure_instance.load_config()
    config.providers["exported_action"].side_effects = True
    procedure_instance.save_config(config)
    service_lock(procedure_instance)
    definition = ProcedureDefinition.model_validate(
        {
            "name": "get_task",
            "contract_in": {
                "fields": {
                    "value": {"type": "int"},
                    "unused": {"type": "json", "optional": True},
                }
            },
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {
                        "value": "$input.value",
                        "metadata": '{"passthrough": true}',
                    },
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 3},
            "declared_tier": "graph_write",
        }
    )

    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )

    assert proposed.warnings == [
        "contract_in field 'unused' is declared but not consumed by any procedure step",
        "procedure name 'get_task' implies a read, but step 'invoke' uses "
        "side-effecting provider 'exported_action'",
        "step 'invoke' input value at 'input.metadata' is a stringified JSON object; "
        "pass the object directly",
        "budget.max_provider_calls (3) exceeds the expanded provider-call count (1); "
        "the extra headroom is unreachable",
    ]


def test_proposal_blocks_input_reference_missing_from_repeat_nested_step(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "retry_with_bad_nested_reference",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "retry",
                    "repeat": {
                        "max_attempts": 2,
                        "until": {
                            "left": "$steps.attempt_result",
                            "op": "eq",
                            "right": 1,
                            "message": "done",
                        },
                        "steps": [
                            {
                                "id": "nested_invoke",
                                "provider": "exported_action",
                                "input": {"value": "$input.transactions_arguments"},
                                "as": "attempt_result",
                            }
                        ],
                    },
                    "as": "attempts",
                }
            ],
            "returns": "attempts",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 2},
            "declared_tier": "graph_write",
        }
    )

    with pytest.raises(ConfigError) as exc_info:
        service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("proposer"),
        )

    message = str(exc_info.value)
    assert "step 'nested_invoke'" in message
    assert "'$input.transactions_arguments'" in message
    assert "value (int, required)" in message


def test_allow_extra_contract_permits_undeclared_input_references(
    procedure_instance: CruxibleInstance,
) -> None:
    """``allow_extra`` contracts accept keys they never declared, so the lint stands down."""
    definition = ProcedureDefinition.model_validate(
        {
            "name": "open_contract_procedure",
            "contract_in": "OpenProcedureInput",
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {"value": "$input.undeclared_but_permitted"},
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )

    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )

    assert proposed.procedure.status == "pending"
    # 'value' is declared but unconsumed; the unused-field warning is suppressed
    # because an allow_extra contract cannot say which keys the payload carries.
    assert proposed.warnings == []


def test_builtin_json_object_contract_permits_undeclared_input_references(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "json_object_procedure",
            "contract_in": "cruxible.JsonObject",
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {"value": "$input.anything_at_all"},
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )

    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )

    assert proposed.warnings == []
    details = service_get_procedure_details(
        procedure_instance,
        proposed.procedure.procedure_id,
    )
    assert details.contract_in_schema is not None
    assert details.contract_in_schema.model_dump(exclude_none=True) == {
        "fields": [],
        "allow_extra": True,
        # An open contract with no declared fields accepts {} plus anything.
        "input_example": {},
    }


def test_empty_input_and_json_object_schemas_are_distinguishable(
    procedure_instance: CruxibleInstance,
) -> None:
    """Both declare no fields; only ``allow_extra`` says whether input is accepted."""

    def _propose(name: str, contract_in: str) -> str:
        definition = ProcedureDefinition.model_validate(
            {
                "name": name,
                "contract_in": contract_in,
                "steps": [
                    {
                        "id": "invoke",
                        "provider": "exported_action",
                        "input": {"value": 1},
                        "as": "result",
                    }
                ],
                "returns": "result",
                "precondition": {},
                "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
                "declared_tier": "graph_write",
            }
        )
        return service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("proposer"),
        ).procedure.procedure_id

    empty_id = _propose("empty_input_procedure", "cruxible.EmptyInput")
    json_id = _propose("json_object_input_procedure", "cruxible.JsonObject")

    empty = service_get_procedure_details(procedure_instance, empty_id).contract_in_schema
    json_object = service_get_procedure_details(procedure_instance, json_id).contract_in_schema
    assert empty is not None and json_object is not None
    assert empty.fields == [] and json_object.fields == []
    assert empty.allow_extra is False
    assert json_object.allow_extra is True
    # The worked example says the same thing a second way: an open contract has
    # a valid empty payload to paste, a closed empty one accepts no payload.
    assert empty.input_example is None
    assert json_object.input_example == {}
    assert empty != json_object


def test_contract_in_schema_reports_defaulted_field_as_not_required(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "defaulted_input_procedure",
            "contract_in": {
                "fields": {
                    "value": {"type": "int"},
                    "mode": {
                        "type": "string",
                        "default": "fast",
                        "enum": ["fast", "slow"],
                        "description": "Execution mode",
                    },
                }
            },
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {"value": "$input.value", "mode": "$input.mode"},
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )
    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )

    schema = service_get_procedure_details(
        procedure_instance,
        proposed.procedure.procedure_id,
    ).contract_in_schema

    assert schema is not None
    assert schema.model_dump(exclude_none=True) == {
        "fields": [
            {
                "name": "mode",
                "type": "string",
                # Contract validation fills the default before it checks
                # optionality, so the caller never has to supply this key.
                "required": False,
                "default": "fast",
                "enum": ["fast", "slow"],
                "description": "Execution mode",
            },
            {"name": "value", "type": "int", "required": True},
        ],
        "allow_extra": False,
        # 'mode' is filled from its default, so the worked example omits it:
        # including it would teach a payload wider than the contract demands.
        "input_example": {"value": 1},
    }


def test_contract_in_schema_carries_a_worked_input_example(
    procedure_instance: CruxibleInstance,
) -> None:
    """A field list still leaves the caller inventing values; the example is pasteable.

    One contract exercising every generation branch, so a branch that stops
    agreeing with runtime validation cannot hide behind the branches that do.
    """
    contract_in: dict[str, Any] = {
        "description": "One task reconciliation request",
        "fields": {
            # A description that quotes a literal.
            "task_id": {"type": "string", "description": "Task identifier, e.g. TSK-1"},
            # An inline vocabulary.
            "mode": {"type": "string", "enum": ["fast", "slow"]},
            # A shared vocabulary resolved through the config's enums.
            "severity": {"type": "string", "enum_ref": "Severity"},
            # Type placeholders.
            "attempts": {"type": "int"},
            "dry_run": {"type": "bool"},
            # Nested json: enums at non-string nodes, an enum_ref, array items,
            # and a plain placeholder underneath them all.
            "spec": {
                "type": "json",
                "json_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "enum": [10, 20]},
                        "ratio": {"type": "number"},
                        "tier": {"type": "string", "enum_ref": "Severity"},
                        "labels": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["alpha", "beta"]},
                        },
                    },
                    "required": ["limit", "ratio", "tier", "labels"],
                },
            },
            # Filled by validation before the optionality check, so never a key
            # the caller must supply.
            "window": {"type": "string", "default": "7d"},
            "note": {"type": "string", "optional": True},
        },
    }

    schema = _contract_in_schema(
        procedure_instance,
        "reconcile_task_with_rich_contract",
        contract_in,
    )

    assert schema.description == "One task reconciliation request"
    assert schema.input_example == {
        "attempts": 1,
        "dry_run": True,
        # An enum pins the value exactly; a description that spells one out is
        # the author's own worked value; otherwise a placeholder for the type.
        "mode": "fast",
        "severity": "low",
        "spec": {"limit": 10, "ratio": 1.0, "tier": "low", "labels": ["alpha"]},
        "task_id": "TSK-1",
    }
    # The optional and defaulted fields are absent: the example teaches the
    # payload the contract demands, not the widest one it tolerates.
    assert "note" not in schema.input_example
    assert "window" not in schema.input_example
    spec_field = next(field for field in schema.fields if field.name == "spec")
    assert spec_field.json_schema == contract_in["fields"]["spec"]["json_schema"]

    # The example is worked, not decorative: it passes the contract it describes.
    _assert_example_satisfies_contract(procedure_instance, contract_in, schema.input_example)


def test_input_example_prefers_an_inline_enum_over_the_type_placeholder(
    procedure_instance: CruxibleInstance,
) -> None:
    """A vocabulary constrains a node of any type, not only a string one.

    The generator used to read ``enum`` only on string nodes, so an integer
    node enumerating ``[7, 8]`` was demonstrated with the type placeholder
    ``1`` -- an example the contract's own validation rejects.
    """
    contract_in = {
        "fields": {
            "retries": {"type": "json", "json_schema": {"type": "integer", "enum": [7, 8]}},
            "ratio": {"type": "json", "json_schema": {"type": "number", "enum": [2.5, 3.5]}},
            "dry_run": {"type": "json", "json_schema": {"type": "boolean", "enum": [False]}},
        }
    }

    schema = _contract_in_schema(procedure_instance, "choose_from_typed_enums", contract_in)

    assert schema.input_example == {"retries": 7, "ratio": 2.5, "dry_run": False}
    _assert_example_satisfies_contract(procedure_instance, contract_in, schema.input_example)


def test_input_example_fills_an_enum_ref_field_from_the_shared_vocabulary(
    procedure_instance: CruxibleInstance,
) -> None:
    """A field pinned to a config-declared enum is demonstrated with a member of it."""
    contract_in = {"fields": {"severity": {"type": "string", "enum_ref": "Severity"}}}

    schema = _contract_in_schema(procedure_instance, "triage_by_shared_severity", contract_in)

    assert schema.input_example == {"severity": "low"}
    _assert_example_satisfies_contract(procedure_instance, contract_in, schema.input_example)


def test_input_example_honors_enums_nested_inside_a_json_field_schema(
    procedure_instance: CruxibleInstance,
) -> None:
    """Every nested node reads its vocabulary: object properties, array items, deeper objects."""
    contract_in = {
        "fields": {
            "spec": {
                "type": "json",
                "json_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "enum": [10, 20]},
                        "tier": {"type": "string", "enum_ref": "Severity"},
                        "codes": {"type": "array", "items": {"type": "integer", "enum": [3, 4]}},
                        "nested": {
                            "type": "object",
                            "properties": {"flag": {"type": "boolean", "enum": [False]}},
                            "required": ["flag"],
                        },
                    },
                    "required": ["limit", "tier", "codes", "nested"],
                },
            }
        }
    }

    schema = _contract_in_schema(procedure_instance, "apply_nested_enum_spec", contract_in)

    assert schema.input_example == {
        "spec": {
            "limit": 10,
            "tier": "low",
            "codes": [3],
            "nested": {"flag": False},
        }
    }
    _assert_example_satisfies_contract(procedure_instance, contract_in, schema.input_example)


def test_input_example_includes_a_required_property_the_schema_never_describes(
    procedure_instance: CruxibleInstance,
) -> None:
    """Presence is what a bare ``required`` entry demands, so the example supplies it."""
    contract_in = {
        "fields": {
            "spec": {
                "type": "json",
                "json_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": ["limit", "opaque"],
                },
            }
        }
    }

    schema = _contract_in_schema(procedure_instance, "apply_partially_described_spec", contract_in)

    assert schema.input_example == {"spec": {"limit": 1, "opaque": "<value>"}}
    _assert_example_satisfies_contract(procedure_instance, contract_in, schema.input_example)


def test_literal_input_text_outside_a_reference_field_is_not_blocked(
    procedure_instance: CruxibleInstance,
) -> None:
    """Lint scope equals resolution scope, so an assert message may quote a ref.

    ``assert.message`` is operator-facing prose the resolver never walks. Reading
    it as a reference would block a definition that runs correctly -- the
    over-blocking failure mode, strictly worse than the under-blocking one this
    lint exists to fix.
    """
    quoted = "supply $input.transactions_arguments before retrying"
    steps = [
        {
            "id": "invoke",
            "provider": "exported_action",
            "input": {"value": "$input.value"},
            "as": "result",
        },
        {
            "id": "guard",
            "assert": {"left": "$steps.result", "op": "ne", "right": 0, "message": quoted},
        },
    ]
    definition = ProcedureDefinition.model_validate(
        {
            "name": "assert_message_quotes_a_reference",
            "contract_in": "ProcedureInput",
            "steps": steps,
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )

    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )
    assert proposed.procedure.status == "pending"
    assert proposed.warnings == []

    # The same text in a position the resolver DOES walk is still refused.
    blocked = ProcedureDefinition.model_validate(
        {
            "name": "assert_left_uses_a_bad_reference",
            "contract_in": "ProcedureInput",
            "steps": [
                steps[0],
                {
                    "id": "guard",
                    "assert": {
                        "left": "$input.transactions_arguments",
                        "op": "ne",
                        "right": 0,
                        "message": "guard",
                    },
                },
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )
    with pytest.raises(ConfigError) as exc_info:
        service_propose_procedure(
            procedure_instance,
            blocked,
            actor_context=actor("proposer"),
        )
    assert "step 'guard'" in str(exc_info.value)
    assert "'$input.transactions_arguments'" in str(exc_info.value)


def test_blocking_message_labels_a_defaulted_field_optional_to_supply(
    procedure_instance: CruxibleInstance,
) -> None:
    """One requiredness predicate: the rejection and the discovery surface agree."""
    definition = ProcedureDefinition.model_validate(
        {
            "name": "defaulted_field_label",
            "contract_in": {
                "fields": {
                    "value": {"type": "int"},
                    "mode": {"type": "string", "default": "fast"},
                }
            },
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {"value": "$input.absent_field"},
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )

    with pytest.raises(ConfigError) as exc_info:
        service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("proposer"),
        )

    message = str(exc_info.value)
    assert "value (int, required)" in message
    # Contract validation fills the default before the optional check, so the
    # caller never has to supply this key -- and the echo must not claim so.
    assert "mode (string, optional)" in message


def test_wholesale_string_passthrough_into_arguments_is_warned(
    procedure_instance: CruxibleInstance,
) -> None:
    """A whole declared string handed to an ``arguments`` bundle defeats the contract."""
    definition = ProcedureDefinition.model_validate(
        {
            "name": "invoke_agent_tool",
            "contract_in": {
                "fields": {
                    "tool_arguments": {"type": "string"},
                    "tool_name": {"type": "string"},
                }
            },
            "steps": [
                {
                    "id": "invoke",
                    "provider": "exported_action",
                    "input": {
                        "name": "$input.tool_name",
                        "arguments": "$input.tool_arguments",
                    },
                    "as": "result",
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 1},
            "declared_tier": "graph_write",
        }
    )

    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )

    # ``tool_name`` goes into a normally named key and is not flagged: the
    # objection is the opaque bundle, not passing a declared field along.
    assert proposed.warnings == [
        "step 'invoke' input at 'input.arguments' passes the whole contract_in field "
        "'tool_arguments' into an 'arguments' parameter; the contract cannot validate "
        "what that string carries -- declare the individual fields the provider needs"
    ]


def _add_read_provider(instance: CruxibleInstance, name: str) -> None:
    """Register a second exported provider that reads rather than writes."""
    config = instance.load_config()
    config.providers[name] = config.providers["exported_action"].model_copy(
        update={"side_effects": False}
    )
    config.providers["exported_action"].side_effects = True
    instance.save_config(config)
    service_lock(instance)


def _provider_step(step_id: str, provider: str) -> dict[str, object]:
    return {
        "id": step_id,
        "provider": provider,
        "input": {"value": 1},
        "as": f"{step_id}_result",
    }


def test_read_fanout_guidance_warns_on_mixed_and_wide_procedures(
    procedure_instance: CruxibleInstance,
) -> None:
    """Reads bundled with a write, or too many provider steps, get split guidance."""
    _add_read_provider(procedure_instance, "read_action")

    mixed = ProcedureDefinition.model_validate(
        {
            "name": "reconcile_task",
            "contract_in": "cruxible.EmptyInput",
            "steps": [
                _provider_step("read_one", "read_action"),
                _provider_step("read_two", "read_action"),
                _provider_step("write_it", "exported_action"),
            ],
            "returns": "write_it_result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 3},
            "declared_tier": "graph_write",
        }
    )
    proposed = service_propose_procedure(
        procedure_instance,
        mixed,
        actor_context=actor("proposer"),
    )
    assert proposed.warnings == [
        "procedure mixes 2 read steps (read_one, read_two) with 1 side-effecting step(s) "
        "(write_it); consider splitting reads into a read-only bundle"
    ]

    wide = ProcedureDefinition.model_validate(
        {
            "name": "reconcile_everything",
            "contract_in": "cruxible.EmptyInput",
            "steps": [_provider_step(f"read_{index}", "read_action") for index in range(6)],
            "returns": "read_5_result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 6},
            "declared_tier": "graph_write",
        }
    )
    wide_proposed = service_propose_procedure(
        procedure_instance,
        wide,
        actor_context=actor("proposer"),
    )
    assert wide_proposed.warnings == [
        "procedure declares 6 provider steps, above the 5-step guidance for one "
        "procedure; consider splitting reads into a read-only bundle"
    ]


def test_contract_in_schema_is_none_when_contract_no_longer_resolves(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition("unresolvable_contract_procedure").model_copy(
        update={"contract_in": "ContractRemovedFromConfig"}
    )
    record = ProcedureRecord(
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition),
        proposed_actor_context=actor("proposer"),
    )
    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_procedure(record)

    details = service_get_procedure_details(procedure_instance, record.procedure_id)

    assert details.procedure.procedure_id == record.procedure_id
    assert details.contract_in_schema is None


def test_acceptance_blocks_a_pending_proposal_that_predates_the_authoring_lint(
    procedure_instance: CruxibleInstance,
) -> None:
    """Proposals stored before the lint shipped are caught at accept, not silently accepted."""
    definition = provider_definition("legacy_pending_bad_reference")
    definition = definition.model_copy(
        update={
            "steps": [
                definition.steps[0].model_copy(
                    update={"input": {"value": "$input.transactions_arguments"}}
                )
            ]
        }
    )
    # Persist directly: propose itself now refuses this definition, so the only
    # way such a record exists is that it predates the lint.
    record = ProcedureRecord(
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition),
        proposed_actor_context=actor("proposer"),
    )
    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_procedure(record)

    with pytest.raises(ConfigError) as exc_info:
        service_accept_procedure(
            procedure_instance,
            record.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer"),
        )

    assert "'$input.transactions_arguments'" in str(exc_info.value)
    assert service_get_procedure(procedure_instance, record.procedure_id).status == "pending"


def test_acceptance_refuses_precondition_entity_type_removed_after_proposal(
    procedure_instance: CruxibleInstance,
) -> None:
    definition = provider_definition(
        "removed_precondition_type",
        precondition={
            "entity_type": "Task",
            "condition": {"status": "ready"},
        },
    )
    proposed = service_propose_procedure(
        procedure_instance,
        definition,
        actor_context=actor("proposer"),
    )
    config = procedure_instance.load_config()
    del config.entity_types["Task"]
    procedure_instance.save_config(config)

    with pytest.raises(
        ConfigError,
        match="precondition references unknown entity type 'Task'",
    ) as exc_info:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    receipt = _receipt(procedure_instance, exc_info.value.mutation_receipt_id)
    assert receipt.committed is False
    assert any(
        "Task" in str(node.detail.get("reason", ""))
        for node in receipt.nodes
        if node.node_type == "validation"
    )
    assert (
        service_get_procedure(procedure_instance, proposed.procedure.procedure_id).status
        == "pending"
    )


def test_acceptance_refuses_same_actor_and_receipts_the_refusal(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition(),
        actor_context=actor("same-person", "op-propose"),
    )

    with pytest.raises(ConfigError, match="independent from the proposer") as exc_info:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("same-person", "op-review"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    receipt = _receipt(procedure_instance, exc_info.value.mutation_receipt_id)
    assert receipt.operation_type == "procedure_transition"
    assert receipt.committed is False
    assert (
        service_get_procedure(procedure_instance, proposed.procedure.procedure_id).status
        == "pending"
    )


def test_missing_proposer_or_reviewer_attribution_is_refused_and_receipted(
    procedure_instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError, match="proposer actor context is required") as propose_exc:
        service_propose_procedure(
            procedure_instance,
            provider_definition("missing_proposer"),
            actor_context=None,
        )
    assert propose_exc.value.mutation_receipt_id is not None

    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("missing_reviewer"),
        actor_context=actor("proposer"),
    )
    with pytest.raises(ConfigError, match="reviewer actor context is required") as review_exc:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=None,
        )
    assert review_exc.value.mutation_receipt_id is not None

    definition = provider_definition("persisted_null_proposer")
    malformed = ProcedureRecord(
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition),
        proposed_actor_context=None,
    )
    with procedure_instance.write_transaction() as uow:
        uow.procedures.save_procedure(malformed)
    with pytest.raises(ConfigError, match="proposer actor context is missing/null") as null_exc:
        service_accept_procedure(
            procedure_instance,
            malformed.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer"),
        )
    assert null_exc.value.mutation_receipt_id is not None


def test_version_conflict_refuses_without_transition(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition(),
        actor_context=actor("proposer"),
    )

    with pytest.raises(ConfigError, match="expected version 2, found 1") as exc_info:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=2,
            actor_context=actor("reviewer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    assert service_get_procedure(procedure_instance, proposed.procedure.procedure_id).version == 1


def test_reject_and_retire_require_reasons(
    procedure_instance: CruxibleInstance,
) -> None:
    rejected_candidate = service_propose_procedure(
        procedure_instance,
        provider_definition("reject_me"),
        actor_context=actor("proposer-a"),
    )
    with pytest.raises(ConfigError, match="reject requires a non-empty reason"):
        service_reject_procedure(
            procedure_instance,
            rejected_candidate.procedure.procedure_id,
            expected_version=1,
            reason="  ",
            actor_context=actor("reviewer-a"),
        )
    rejected = service_reject_procedure(
        procedure_instance,
        rejected_candidate.procedure.procedure_id,
        expected_version=1,
        reason="unsafe composition",
        actor_context=actor("reviewer-a"),
    )
    assert rejected.procedure.status == "rejected"
    assert rejected.procedure.version == 2
    assert rejected.procedure.reason == "unsafe composition"

    live_candidate = service_propose_procedure(
        procedure_instance,
        provider_definition("retire_me"),
        actor_context=actor("proposer-b"),
    )
    live = service_accept_procedure(
        procedure_instance,
        live_candidate.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer-b"),
    )
    with pytest.raises(ConfigError, match="retire requires a non-empty reason"):
        service_retire_procedure(
            procedure_instance,
            live.procedure.procedure_id,
            expected_version=2,
            reason="",
            actor_context=actor("retirer"),
        )
    retired = service_retire_procedure(
        procedure_instance,
        live.procedure.procedure_id,
        expected_version=2,
        reason="action is obsolete",
        actor_context=actor("retirer"),
    )
    assert retired.procedure.status == "retired"
    assert retired.procedure.version == 3
    assert retired.procedure.reason == "action is obsolete"
    assert retired.procedure.retired_actor_context == actor("retirer")


def test_live_change_is_new_superseding_proposal_and_acceptance_retires_old(
    procedure_instance: CruxibleInstance,
) -> None:
    first_pending = service_propose_procedure(
        procedure_instance,
        provider_definition("versioned_action"),
        actor_context=actor("first-proposer"),
    )
    first_live = service_accept_procedure(
        procedure_instance,
        first_pending.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("first-reviewer"),
    )
    changed = provider_definition("versioned_action").model_copy(
        update={"description": "A separately reviewed revision"}
    )
    second_pending = service_propose_procedure(
        procedure_instance,
        changed,
        actor_context=actor("second-proposer"),
        supersedes_procedure_id=first_live.procedure.procedure_id,
    )

    assert second_pending.procedure.procedure_id != first_live.procedure.procedure_id
    assert second_pending.procedure.supersedes_procedure_id == first_live.procedure.procedure_id
    assert (
        service_get_procedure(procedure_instance, first_live.procedure.procedure_id).status
        == "live"
    )

    second_live = service_accept_procedure(
        procedure_instance,
        second_pending.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("second-reviewer"),
    )

    retired_first = service_get_procedure(procedure_instance, first_live.procedure.procedure_id)
    assert second_live.procedure.status == "live"
    assert retired_first.status == "retired"
    assert retired_first.version == 3
    assert retired_first.reason == (
        f"superseded by procedure '{second_live.procedure.procedure_id}'"
    )


def test_provider_export_and_declared_tier_are_revalidated_at_proposal(
    procedure_instance: CruxibleInstance,
) -> None:
    disabled = provider_definition("disabled_provider").model_copy(deep=True)
    assert disabled.steps[0].provider == "exported_action"  # type: ignore[union-attr]
    disabled.steps[0].provider = "disabled_action"  # type: ignore[union-attr]
    with pytest.raises(ConfigError, match="not exported to procedures"):
        service_propose_procedure(
            procedure_instance,
            disabled,
            actor_context=actor("proposer"),
        )

    low_tier = provider_definition("low_tier").model_copy(
        update={"declared_tier": "governed_write"}
    )
    with pytest.raises(ConfigError, match="below its effective provider tier") as tier_exc:
        service_propose_procedure(
            procedure_instance,
            low_tier,
            actor_context=actor("proposer"),
        )
    # The floor is not visible in the definition the author wrote: it comes from
    # a provider's procedure_access. Naming the provider that raised it, and the
    # tiers that clear it, makes the refusal fixable in one edit instead of by
    # bisecting the referenced provider list.
    tier_message = str(tier_exc.value)
    assert "required by provider 'exported_action'" in tier_message
    assert "procedure_access 'graph_write'" in tier_message
    assert "set declared_tier to one of: graph_write, admin" in tier_message


def test_acceptance_recompiles_and_refuses_a_provider_deexported_after_proposal(
    procedure_instance: CruxibleInstance,
) -> None:
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("drifted_provider"),
        actor_context=actor("proposer"),
    )
    config = procedure_instance.load_config()
    config.providers["exported_action"].procedure_access = "disabled"
    procedure_instance.save_config(config)
    service_lock(procedure_instance)

    with pytest.raises(ConfigError, match="not exported to procedures") as exc_info:
        service_accept_procedure(
            procedure_instance,
            proposed.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer"),
        )

    assert exc_info.value.mutation_receipt_id is not None
    assert (
        service_get_procedure(procedure_instance, proposed.procedure.procedure_id).status
        == "pending"
    )


def test_proposer_rejecting_own_proposal_still_records_a_reject_not_a_withdrawal(
    procedure_instance: CruxibleInstance,
) -> None:
    """``reject`` stays a reviewer verdict even when the proposer issues it.

    ``withdraw`` is the author's own retraction and lands in ``withdrawn``;
    nothing collapses the two, so a record still says which one happened.
    """
    proposed = service_propose_procedure(
        procedure_instance,
        provider_definition("reject_my_own"),
        actor_context=actor("proposer"),
    )
    rejected = service_reject_procedure(
        procedure_instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        reason="rejecting my own proposal",
        actor_context=actor("proposer"),
    )
    assert rejected.procedure.status == "rejected"
    assert rejected.procedure.reason == "rejecting my own proposal"


def test_acceptance_refuses_second_live_procedure_with_same_name(
    procedure_instance: CruxibleInstance,
) -> None:
    first = service_propose_procedure(
        procedure_instance,
        provider_definition("unique_name"),
        actor_context=actor("proposer-a"),
    )
    service_accept_procedure(
        procedure_instance,
        first.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer-a"),
    )
    second = service_propose_procedure(
        procedure_instance,
        provider_definition("unique_name"),
        actor_context=actor("proposer-b"),
    )
    with pytest.raises(ConfigError, match="one live version per name") as exc_info:
        service_accept_procedure(
            procedure_instance,
            second.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer-b"),
        )
    assert exc_info.value.mutation_receipt_id is not None
    assert (
        service_get_procedure(procedure_instance, second.procedure.procedure_id).status == "pending"
    )


def test_supersede_race_second_acceptance_refused(
    procedure_instance: CruxibleInstance,
) -> None:
    v1 = service_propose_procedure(
        procedure_instance,
        provider_definition("raced_name"),
        actor_context=actor("proposer-a"),
    )
    v1_live = service_accept_procedure(
        procedure_instance,
        v1.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer-a"),
    )
    v2a = service_propose_procedure(
        procedure_instance,
        provider_definition("raced_name"),
        actor_context=actor("proposer-b"),
        supersedes_procedure_id=v1_live.procedure.procedure_id,
    )
    v2b = service_propose_procedure(
        procedure_instance,
        provider_definition("raced_name"),
        actor_context=actor("proposer-c"),
        supersedes_procedure_id=v1_live.procedure.procedure_id,
    )
    service_accept_procedure(
        procedure_instance,
        v2a.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("reviewer-b"),
    )
    with pytest.raises(ConfigError, match="one live version per name"):
        service_accept_procedure(
            procedure_instance,
            v2b.procedure.procedure_id,
            expected_version=1,
            actor_context=actor("reviewer-c"),
        )
    live_rows = [
        record
        for record in (
            service_get_procedure(procedure_instance, pid)
            for pid in (
                v1_live.procedure.procedure_id,
                v2a.procedure.procedure_id,
                v2b.procedure.procedure_id,
            )
        )
        if record.status == "live"
    ]
    assert len(live_rows) == 1
    assert live_rows[0].procedure_id == v2a.procedure.procedure_id
