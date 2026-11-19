"""Native Contract record construction shares operation boundary validation."""

from dataclasses import FrozenInstanceError

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.provider_operations import OperationValueRefused
from cruxible_client.contracts.records import RecordConstructor, record_field_names
from cruxible_client.contracts.shapes import Contract


def contract(name="test.record", **fields):
    return Contract.model_validate({"name": name, "version": 1, "fields": fields})


def test_record_preserves_exact_wire_and_schema():
    schema = contract(url="string", format="enum[json, text]", max_bytes="int?")
    before = schema.model_dump(mode="json")
    record = RecordConstructor(schema)(url="https://example.test", format="json")
    assert canonical_bytes(record) == canonical_bytes(
        {"url": "https://example.test", "format": "json"}
    )
    assert schema.model_dump(mode="json") == before
    with pytest.raises(FrozenInstanceError):
        record.url = "changed"


@pytest.mark.parametrize("fields", [{"count": True}, {"count": "2"}, {}, {"count": 2, "typo": 1}])
def test_record_refuses_wrong_missing_and_unknown_fields(fields):
    with pytest.raises(OperationValueRefused):
        RecordConstructor(contract(count="int"))(**fields)


def test_nested_records_and_lists_are_typed_and_defensive():
    row = contract("test.row", count="int{0..10}")
    schema = contract(source="test.row@1", rows="list<test.row@1>{max_items: 3}")
    constructor = RecordConstructor(
        schema, _resolve=lambda ref: row if ref.name == row.name else None
    )
    record = constructor(source=constructor.source(count=2), rows=[constructor.rows(count=3)])
    assert record.source.count == 2 and record.rows[0].count == 3
    record["rows"].append({"count": 4})
    assert len(record.rows) == 1
    with pytest.raises(OperationValueRefused):
        constructor.source(count=11)
    with pytest.raises(AttributeError):
        _ = record.source.missing


def test_optional_absence_is_not_null():
    constructor = RecordConstructor(contract(note="string?"))
    assert constructor().model_dump() == {}
    with pytest.raises(AttributeError, match="absent"):
        _ = constructor().note
    with pytest.raises(OperationValueRefused):
        constructor(note=None)


def test_field_aliases_preserve_wire_keys():
    schema = contract(**{name: "string" for name in ("items", "class", "field_6974656d73")})
    aliases = record_field_names(schema)
    value = RecordConstructor(schema)(**{alias: wire for alias, wire in aliases.items()})
    assert {getattr(value, alias) for alias in aliases} == set(schema.fields)
    assert value["items"] == "items"


def test_native_constraints_are_enforced():
    constructor = RecordConstructor(contract(mode="enum[json]", count="int{1..3}"))
    for fields in (
        {"mode": "xml", "count": 2},
        {"mode": "json", "count": 4},
        {"mode": "json", "count": True},
    ):
        with pytest.raises(OperationValueRefused):
            constructor(**fields)


def test_untyped_record_shapes_are_refused():
    with pytest.raises(ValueError):
        contract(payload="json")
