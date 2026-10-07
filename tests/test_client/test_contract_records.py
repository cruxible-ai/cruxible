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


# The exact workspace.file (version 2) schemas from cruxible-provider-workspace:
# its output content is a oneOf over a text shape and a bytes shape, and its
# input byte_length carries a minimum.
WORKSPACE_FILE_CONTENT = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "text"},
                "encoding": {"const": "utf-8"},
                "bom": {"type": "boolean"},
                "newline": {"type": "string", "enum": ["lf", "crlf", "cr", "mixed", "none"]},
                "trailing_newline": {"type": "boolean"},
                "line_count": {"type": "integer"},
                "character_count": {"type": "integer"},
                "text": {"type": "string"},
                "lines": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "kind",
                "encoding",
                "bom",
                "newline",
                "trailing_newline",
                "line_count",
                "character_count",
                "text",
                "lines",
            ],
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "bytes"},
                "encoding": {"const": "base64"},
                "byte_length": {"type": "integer"},
                "bytes": {"type": "string"},
            },
            "required": ["kind", "encoding", "byte_length", "bytes"],
        },
    ],
}
WORKSPACE_FILE_BYTE_LENGTH = {"type": "integer", "minimum": 0}


def _workspace_file_record():
    return RecordConstructor(
        ContractSchema(
            fields={
                "content": PropertySchema(type="json", json_schema=WORKSPACE_FILE_CONTENT),
                "byte_length": PropertySchema(
                    type="json", json_schema=WORKSPACE_FILE_BYTE_LENGTH, optional=True
                ),
            }
        )
    )


TEXT_CONTENT = {
    "kind": "text",
    "encoding": "utf-8",
    "bom": False,
    "newline": "lf",
    "trailing_newline": True,
    "line_count": 1,
    "character_count": 3,
    "text": "hi\n",
    "lines": ["hi"],
}
BYTES_CONTENT = {"kind": "bytes", "encoding": "base64", "byte_length": 2, "bytes": "AAE="}


def test_workspace_file_oneof_and_minimum_schemas_type_real_text_and_bytes_values():
    record = _workspace_file_record()
    assert record(content=TEXT_CONTENT, byte_length=0)["content"]["kind"] == "text"
    assert record(content=BYTES_CONTENT, byte_length=2)["content"]["kind"] == "bytes"


def test_a_value_matching_two_oneof_variants_is_refused():
    constructor = RecordConstructor(
        ContractSchema(
            fields={
                "value": PropertySchema(
                    type="json",
                    json_schema={
                        "type": "object",
                        "oneOf": [
                            {"type": "object", "properties": {"a": {"type": "string"}}},
                            {"type": "object", "properties": {"b": {"type": "string"}}},
                        ],
                    },
                )
            }
        )
    )
    assert constructor(value={"a": "x", "b": 1})["value"] == {"a": "x", "b": 1}
    with pytest.raises(ProcedureContractValidationError):
        constructor(value={"a": "x"})


def test_a_value_matching_no_oneof_variant_is_refused():
    with pytest.raises(ProcedureContractValidationError):
        _workspace_file_record()(content={**BYTES_CONTENT, "kind": "text"})


def test_a_negative_byte_length_is_refused():
    with pytest.raises(ProcedureContractValidationError):
        _workspace_file_record()(content=TEXT_CONTENT, byte_length=-1)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "oneOf": [{"type": "string"}]},
        {"type": "integer", "anyOf": [{"type": "integer"}, {"type": "null"}]},
        {"type": "string", "minimum": 0},
        {"type": "integer", "minimum": 0.5},
        {"oneOf": [{"type": "string"}], "anyOf": [{"type": "string"}]},
        {"oneOf": [{"type": "string"}], "minLength": 1},
    ],
)
def test_unrepresentable_union_and_bound_schemas_are_refused(schema):
    with pytest.raises(RecordSchemaError):
        RecordConstructor(
            ContractSchema(fields={"value": PropertySchema(type="json", json_schema=schema)})
        )


def test_source_assignability_reads_oneof_like_anyof():
    from cruxible_client.contracts.procedures.source_compiler import _assignable

    text = PropertySchema(type="json", json_schema=WORKSPACE_FILE_CONTENT["oneOf"][0])
    union = PropertySchema(type="json", json_schema=WORKSPACE_FILE_CONTENT)
    assert _assignable(text, union)
    assert not _assignable(union, text)


def test_source_assignability_refuses_an_overlapping_oneof_variant():
    from cruxible_client.contracts.procedures.source_compiler import _assignable

    overlapping = PropertySchema(
        type="json",
        json_schema={
            "oneOf": [
                {"type": "string", "enum": ["a", "b"]},
                {"type": "string", "enum": ["b", "c"]},
            ]
        },
    )
    # "b" satisfies both variants, so it fails the oneOf; "a" satisfies one.
    assert not _assignable(PropertySchema(type="string", enum=["b"]), overlapping)
    assert _assignable(PropertySchema(type="string", enum=["a"]), overlapping)
    # An open string might be "b": exclusivity is unproved, so it refuses.
    assert not _assignable(PropertySchema(type="string"), overlapping)
    with pytest.raises(ProcedureContractValidationError):
        RecordConstructor(ContractSchema(fields={"value": overlapping}))(value="b")
