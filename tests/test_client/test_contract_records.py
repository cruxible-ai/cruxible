"""Typed constructors obey the same schema and preserve exact canonical bytes."""

from dataclasses import FrozenInstanceError

import pytest

from cruxible_client.authoring.inputs import CarriedContractInput
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.contracts import ProcedureContractValidationError
from cruxible_client.contracts.records import RecordConstructor, RecordSchemaError


def test_carried_record_reuses_schema_without_changing_authoring_or_wire():
    contract = CarriedContractInput(
        name="fetch",
        fields={
            "url": PropertySchema(type="string"),
            "format": PropertySchema(type="string", enum=["json", "text"]),
            "max_bytes": PropertySchema(type="integer", optional=True, default=1024),
        },
    )
    before = contract.model_dump(mode="json")
    record = contract.value(url="https://example.test", format="json")
    assert record.url == "https://example.test"
    assert record.max_bytes == 1024
    assert canonical_bytes(record) == canonical_bytes(
        {"url": "https://example.test", "format": "json", "max_bytes": 1024}
    )
    assert contract.model_dump(mode="json") == before
    with pytest.raises(FrozenInstanceError):
        record.url = "changed"


@pytest.mark.parametrize(
    "fields",
    [
        {"count": True},
        {"count": "2"},
        {},
        {"count": 2, "typo": 1},
    ],
)
def test_record_refuses_wrong_types_missing_and_unknown_fields(fields):
    constructor = CarriedContractInput(
        name="count", fields={"count": PropertySchema(type="integer")}
    ).value
    with pytest.raises(ProcedureContractValidationError):
        constructor(**fields)


def test_nested_records_validate_the_declared_schema_and_keep_open_json_open():
    constructor = RecordConstructor(
        ContractSchema(
            fields={
                "source": PropertySchema(
                    type="json",
                    json_schema={
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["inline"]},
                            "body": {"type": "string", "minLength": 1},
                        },
                        "required": ["kind", "body"],
                        "additionalProperties": False,
                    },
                ),
                "opaque": PropertySchema(type="json"),
            }
        )
    )
    source = constructor.source(kind="inline", body="example")
    record = constructor(source=source, opaque={"text": "untyped"})
    assert record.source.body == "example"
    assert isinstance(record.opaque, dict)
    record.opaque["text"] = "mutated"
    assert record.opaque["text"] == "untyped"
    with pytest.raises(ProcedureContractValidationError):
        constructor.source(kind="inline", body="")
    with pytest.raises(ProcedureContractValidationError):
        constructor(source={"kind": "inline", "body": 7}, opaque={})
    with pytest.raises(AttributeError):
        constructor.opaque()
    with pytest.raises(AttributeError):
        record.source.not_declared


def test_list_values_are_defensive_and_nested_fields_are_typed():
    schema = ContractSchema(
        fields={
            "rows": PropertySchema(type="list", item_fields={"count": PropertySchema(type="int")})
        }
    )
    constructor = RecordConstructor(schema)
    record = constructor(rows=[constructor.rows(count=3)])
    assert record.rows[0].count == 3
    record["rows"].append({"count": 4})
    assert len(record.rows) == 1
    schema.fields.clear()
    assert constructor(rows=[]).rows == ()


def test_optional_absence_is_not_fabricated_null():
    constructor = RecordConstructor(
        ContractSchema(fields={"note": PropertySchema(type="string", optional=True)})
    )
    record = constructor()
    assert record.model_dump() == {}
    with pytest.raises(AttributeError, match="absent"):
        _ = record.note
    assert constructor(note=None).note is None


def test_escaped_names_do_not_shadow_mapping_methods_or_each_other():
    from cruxible_client.contracts.records import record_field_names

    schema = ContractSchema(
        fields={
            name: PropertySchema(type="string") for name in ("items", "class", "field_6974656d73")
        }
    )
    constructor = RecordConstructor(schema)
    aliases = record_field_names(schema)
    value = constructor(**{alias: wire for alias, wire in aliases.items()})
    assert len(aliases) == 3
    assert {getattr(value, alias) for alias in aliases} == set(schema.fields)
    assert value["items"] == "items"


def test_unrepresented_json_schema_never_silently_accepts_data():
    with pytest.raises(RecordSchemaError, match="minimum"):
        RecordConstructor(
            ContractSchema(
                fields={"bounded": PropertySchema(type="json", json_schema={"minimum": 2})}
            )
        )


def test_invalid_enum_and_canonical_float_are_refused():
    constructor = RecordConstructor(
        ContractSchema(fields={"mode": PropertySchema(type="string", enum=["json"])})
    )
    with pytest.raises(ProcedureContractValidationError):
        constructor(mode="xml")
    with pytest.raises(RecordSchemaError):
        RecordConstructor(ContractSchema(fields={"value": PropertySchema(type="number")}))


def test_nullable_field_preserves_required_presence_and_nested_constraints():
    from copy import deepcopy

    schema = {
        "type": "object",
        "properties": {
            "text": {"type": ["string", "null"]},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "choice": {"anyOf": [{"type": "null"}, {"type": "integer", "enum": [1, 2]}]},
        },
        "required": ["text", "headers", "choice"],
        "additionalProperties": False,
    }
    constructor = RecordConstructor.from_json_schema(schema)
    record = constructor(text=None, headers={"content-type": "application/json"}, choice=1)
    assert record.text is None
    assert deepcopy(record) == record
    for bad in (
        dict(headers={}, choice=None),  # null is permitted, absence is not
        dict(text=4, headers={}, choice=None),
        dict(text=None, headers={"content-type": 2}, choice=None),
        dict(text=None, headers={}, choice=True),
        dict(text=None, headers={}, choice=3),
    ):
        with pytest.raises(ProcedureContractValidationError):
            constructor(**bad)
