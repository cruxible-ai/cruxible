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
    RetireChange,
    RetireRequest,
    SelfEvidence,
    SetChange,
    SetRequest,
    SlotRef,
    WriteOutcome,
    WriteRequest,
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
        WriteRequest.model_validate({"because": "why", "changes": [change]})


def test_scalar_values_keep_their_types() -> None:
    assert SetChange(subject="a/b", field="f", value=True).value is True
    assert type(SetChange(subject="a/b", field="f", value=1).value) is int
    assert type(SetChange(subject="a/b", field="f", value=1.5).value) is float


def test_set_and_retire_are_batches_of_one() -> None:
    lowered = as_write_request(
        SetRequest(because="why", subject="dev.item/a", field="status", value="done", at="0" * 12)
    )
    assert lowered.changes == (SetChange(subject="dev.item/a", field="status", value="done"),)
    assert lowered.at == "0" * 12 and lowered.because == "why"
    retired = as_write_request(
        RetireRequest(because="gone", target=_CLAIM, reason="was-wrong", dry_run=True)
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
        SetRequest(subject="dev.item/a", field="f", value="x", because="y", expect="w")
    )
    assert lowered.changes[0].expect == "w"  # type: ignore[union-attr]
    retired = as_write_request(RetireRequest(target=_CLAIM, because="y", expect=["w"]))
    assert retired.changes[0].expect == ("w",)  # type: ignore[union-attr]


def test_a_write_may_name_its_subject_once() -> None:
    request = WriteRequest.model_validate(
        {
            "because": "x",
            "subject": "dev.item/a",
            "changes": [
                {"op": "set", "field": "status", "value": "done"},
                {"op": "retire", "target": {"field": "status"}},
            ],
        }
    )
    assert request.subject == "dev.item/a"
    assert request.changes[0].subject is None  # type: ignore[union-attr]
    target = request.changes[1].target  # type: ignore[union-attr]
    assert isinstance(target, SlotRef) and target.subject is None
    with pytest.raises(ValidationError):
        WriteRequest.model_validate(
            {"because": "x", "subject": "no-slash", "changes": [{"op": "retire", "target": _CLAIM}]}
        )


def test_capture_evidence_takes_a_handle_and_contract_evidence_a_name() -> None:
    from cruxible_client.contracts.write import ContractEvidence, capture_handle

    evidence = TypeAdapter(tuple[Evidence, ...]).validate_python(
        [
            {"kind": "capture", "capture": "CAP-0123456789ab"},
            {"kind": "capture", "capture": "CAP-" + "0" * 64},
            {"kind": "contract", "contract": "repo.reports"},
        ]
    )
    assert [type(item) for item in evidence] == [CaptureEvidence, CaptureEvidence, ContractEvidence]
    for bad in ("CAP-0123", "CAP-0123456789AB", "cap-0123456789ab", "sha256:abc"):
        with pytest.raises(ValidationError):
            CaptureEvidence(capture=bad)
    with pytest.raises(ValidationError):
        TypeAdapter(Evidence).validate_python({"kind": "contract", "contract": ""})
    assert capture_handle("sha256:" + "0123456789ab" + "f" * 52) == "CAP-0123456789ab"
    assert capture_handle("sha256:" + "a" * 64, length=16) == "CAP-" + "a" * 16


def test_a_warning_is_one_flat_variant_per_code() -> None:
    from cruxible_client.contracts.write import (
        NewerCaptureNotCitableWarning,
        VerdictNotSupportedWarning,
        WriteWarning,
    )

    adapter = TypeAdapter(WriteWarning)
    verdict = adapter.validate_python(
        {
            "code": "playbill.write.verdict_not_supported",
            "change": 0,
            "verdict": "uncovered",
            "message": "m",
        }
    )
    assert isinstance(verdict, VerdictNotSupportedWarning)
    newer = adapter.validate_python(
        {
            "code": "playbill.write.newer_capture_not_citable",
            "change": 0,
            "capture": "CAP-0123456789ab",
            "message": "m",
        }
    )
    assert isinstance(newer, NewerCaptureNotCitableWarning)
    invalid = [
        # An R05 warning without the verdict it is about.
        {"code": "playbill.write.verdict_not_supported", "change": 0, "message": "m"},
        # An uncitable-Capture warning with a verdict and no Capture.
        {
            "code": "playbill.write.newer_capture_not_citable",
            "change": 0,
            "verdict": "uncovered",
            "message": "m",
        },
        # Each variant forbids the other's field.
        {
            "code": "playbill.write.verdict_not_supported",
            "change": 0,
            "verdict": "uncovered",
            "capture": "CAP-0123456789ab",
            "message": "m",
        },
        {
            "code": "playbill.write.newer_capture_not_citable",
            "change": 0,
            "capture": "sha256:" + "0" * 64,
            "message": "m",
        },
        {"code": "playbill.write.something_else", "change": 0, "message": "m"},
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            adapter.validate_python(payload)
        with pytest.raises(ValidationError):
            WriteOutcome.model_validate(
                {
                    "status": "accepted",
                    "coordinate": {"git_oid": "0123456789ab", "generation": 1},
                    "warnings": [payload],
                }
            )


def test_the_outcome_schema_discriminates_warnings_by_code() -> None:
    schema = WriteOutcome.model_json_schema()
    defs = schema["$defs"]
    items = schema["properties"]["warnings"]["items"]
    assert items["discriminator"]["propertyName"] == "code"
    assert set(items["discriminator"]["mapping"]) == {
        "playbill.write.verdict_not_supported",
        "playbill.write.newer_capture_not_citable",
    }
    verdict = defs["VerdictNotSupportedWarning"]
    newer = defs["NewerCaptureNotCitableWarning"]
    assert "verdict" in verdict["required"] and "capture" not in verdict["properties"]
    assert "capture" in newer["required"] and "verdict" not in newer["properties"]
    assert verdict["additionalProperties"] is False and newer["additionalProperties"] is False


def test_an_at_sign_subject_is_the_same_subject_reference() -> None:
    """``@kind/id`` validates as ``kind/id`` wherever a Subject is named."""

    from cruxible_client.contracts.write import subject_reference

    assert subject_reference("@dev.roadmap_item/x") == "dev.roadmap_item/x"
    assert subject_reference("dev.roadmap_item/x") == "dev.roadmap_item/x"
    # Only a Subject reference loses the sigil; any other text is left alone.
    assert subject_reference("@not a subject") == "@not a subject"
    assert subject_reference("@@dev.roadmap_item/x") == "@@dev.roadmap_item/x"
    assert SetChange(subject="@dev.roadmap_item/x", field="title", value="t").subject == (
        "dev.roadmap_item/x"
    )
    assert SlotRef(subject="@dev.roadmap_item/x", field="title").subject == "dev.roadmap_item/x"
    with pytest.raises(ValidationError):
        SetChange(subject="@@dev.roadmap_item/x", field="title", value="t")
