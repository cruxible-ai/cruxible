"""The typed write contracts: discriminated unions, patterns, and the batch lowering."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.write import (
    AddChange,
    CaptureEvidence,
    Change,
    Evidence,
    FileEvidence,
    PlaybillRetireRequestV1,
    PlaybillSetRequestV1,
    PlaybillWriteRequestV1,
    RetireChange,
    SelfEvidence,
    SetChange,
    SlotRef,
    WriteOutcome,
    as_write_request,
)

_CLAIM = "CLM-" + "a" * 32


def test_changes_discriminate_on_op_and_evidence_on_kind() -> None:
    changes = TypeAdapter(tuple[Change, ...]).validate_python(
        [
            {"op": "set", "subject": "dev.item/a", "field": "status", "value": "done"},
            {"op": "add", "subject": "dev.item/a", "field": "governs", "value": "dev.item/b"},
            {"op": "retire", "target": _CLAIM},
            {"op": "retire", "target": {"subject": "dev.item/a", "field": "status"}},
        ]
    )
    assert [type(item) for item in changes] == [SetChange, AddChange, RetireChange, RetireChange]
    assert isinstance(changes[3], RetireChange) and isinstance(changes[3].target, SlotRef)
    evidence = TypeAdapter(tuple[Evidence, ...]).validate_python(
        [
            {"kind": "self", "self": "I saw it."},
            {"kind": "capture", "capture": "sha256:" + "b" * 64},
            {"kind": "file", "file": "docs/plan.md#Status: done"},
        ]
    )
    assert [type(item) for item in evidence] == [SelfEvidence, CaptureEvidence, FileEvidence]
    file_evidence = evidence[2]
    assert isinstance(file_evidence, FileEvidence)
    assert (file_evidence.path, file_evidence.anchor) == ("docs/plan.md", "Status: done")


@pytest.mark.parametrize(
    "change",
    [
        {"op": "set", "subject": "no-slash", "field": "status", "value": "x"},
        {"op": "set", "subject": "dev.item/a", "field": "", "value": "x"},
        {"op": "set", "subject": "dev.item/a", "field": "f", "value": {"nested": 1}},
        {"op": "retire", "target": "CLM-short"},
        {"op": "move", "subject": "dev.item/a"},
        {"op": "set", "subject": "dev.item/a", "field": "f", "value": 1, "extra": True},
    ],
)
def test_malformed_changes_refuse_at_the_schema(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PlaybillWriteRequestV1.model_validate({"because": "why", "changes": [change]})


def test_scalar_values_keep_their_types() -> None:
    assert SetChange(subject="a/b", field="f", value=True).value is True
    assert type(SetChange(subject="a/b", field="f", value=1).value) is int
    assert type(SetChange(subject="a/b", field="f", value=1.5).value) is float


def test_set_and_retire_are_batches_of_one() -> None:
    lowered = as_write_request(
        PlaybillSetRequestV1(
            because="why", subject="dev.item/a", field="status", value="done", at="0" * 12
        )
    )
    assert lowered.changes == (SetChange(subject="dev.item/a", field="status", value="done"),)
    assert lowered.at == "0" * 12 and lowered.because == "why"
    retired = as_write_request(
        PlaybillRetireRequestV1(because="gone", target=_CLAIM, reason="was-wrong", dry_run=True)
    )
    assert retired.changes == (RetireChange(target=_CLAIM, reason="was-wrong"),)
    assert retired.dry_run is True


def test_outcome_omits_empty_fields_and_says_when_it_refused() -> None:
    outcome = WriteOutcome.model_validate(
        {
            "status": "would_refuse",
            "coordinate": {"git_oid": "a" * 12, "generation": 3},
            "refusal": {"code": "playbill.write.unknown_field", "message": "no field"},
        }
    )
    assert outcome.refused
    dumped = outcome.model_dump(mode="json")
    assert set(dumped) == {"tag", "status", "changes", "coordinate", "refusal"}


def test_write_refusal_error_carries_its_repair() -> None:
    error = WriteRefusalError(
        "playbill.write.value_not_member",
        "'dne' is not a member",
        change=0,
        candidates=("done",),
        repair_line="Use one of: blocked, done, ready",
    )
    assert error.error_code == "playbill.write.value_not_member"
    assert "nearest: done" in str(error) and "Use one of" in str(error)
    assert error.context == {
        "change": 0,
        "candidates": ["done"],
        "repair_line": "Use one of: blocked, done, ready",
    }


def test_one_slot_ref_names_a_subject_field_and_procedure_slots_are_named_apart() -> None:
    """R11: one SlotRef, the write contract's, everywhere; a Procedure slot has its own name."""

    import cruxible_client
    from cruxible_client.authoring import sdk, sdk_types

    assert cruxible_client.SlotRef is SlotRef
    assert sdk.SlotRef is SlotRef
    assert not hasattr(sdk_types, "SlotRef")
    assert not hasattr(sdk, "WriteSlotRef")
    assert cruxible_client.ProcedureSlotRef is sdk_types.ProcedureSlotRef


def test_expect_is_one_value_or_every_value_and_travels_through_the_batch() -> None:
    one = SetChange(subject="dev.item/a", field="n", value=2, expect=1)
    assert one.expect == 1 and not isinstance(one.expect, bool)
    flag = SetChange(subject="dev.item/a", field="f", value=False, expect=True)
    assert flag.expect is True
    many = TypeAdapter(Change).validate_python(
        {"op": "retire", "target": _CLAIM, "expect": ["dev.item/b", "dev.item/c"]}
    )
    assert isinstance(many, RetireChange) and many.expect == ("dev.item/b", "dev.item/c")
    none = SetChange(subject="dev.item/a", field="f", value="x", expect=[])
    assert none.expect == ()
    assert SetChange(subject="dev.item/a", field="f", value="x").expect is None
    add = AddChange(subject="dev.item/a", field="g", value="dev.item/b", expect_absent=True)
    assert add.expect_absent
    with pytest.raises(ValidationError):
        SetChange(subject="dev.item/a", field="f", value="x", expect={"nested": 1})  # type: ignore[arg-type]
    lowered = as_write_request(
        PlaybillSetRequestV1(subject="dev.item/a", field="f", value="x", because="y", expect="w")
    )
    assert lowered.changes[0].expect == "w"  # type: ignore[union-attr]
    retired = as_write_request(PlaybillRetireRequestV1(target=_CLAIM, because="y", expect=["w"]))
    assert retired.changes[0].expect == ("w",)  # type: ignore[union-attr]
