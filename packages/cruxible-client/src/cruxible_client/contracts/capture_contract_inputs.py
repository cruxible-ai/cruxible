"""Authored CaptureContracts; the engine expands the closed shape vocabulary."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    CanonicalDuration,
    CaptureContract,
    CaptureContractAny,
    CaptureContractV1,
    CaptureDecoding,
    CaptureFormatError,
    CaptureReplay,
    CaptureRetentionErasurePolicy,
    CaptureSelectionBudget,
    CaptureShape,
    authorize_erasure,
    builtin_contract_for_write,
    capture_contract_digest,
    capture_contract_path,
    capture_contract_successor_break,
    capture_retention_policy,
    capture_shape_components,
)
from cruxible_client.contracts.temporal import parse_iso_duration_microseconds


class CaptureContractInputRecord(BaseModel):
    """A new name and shape, or a merge patch of an accepted contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")
    name: str
    shape: CaptureShape | None = None
    grade: Literal["observed", "derived", "predicted"] | None = None
    replay: CaptureReplay | None = None
    retention: str | None = None
    erasable: bool | None = None
    max_bytes: int | None = Field(default=None, ge=1)
    selector_privacy: Literal["direct_allowed", "pseudonymous_required"] | None = None
    decoding: CaptureDecoding | None = Field(
        default=None,
        description="How the bytes decode for an extract: {format: json|csv|text|bytes, "
        "encoding: identity|gzip, csv?: {comment_prefix, header}, limits: {decoded_bytes, "
        "depth, records}}. Required for any CaptureContract an extract reads.",
    )
    lifecycle: Literal["live", "retired"] | None = None

    @model_serializer(mode="wrap")
    def _merge_patch_wire(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        payload = cast(dict[str, object], handler(self))
        return {name: value for name, value in payload.items() if name in self.model_fields_set}

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        capture_contract_path(value)
        return value

    @field_validator("retention")
    @classmethod
    def _retention(cls, value: str | None) -> str | None:
        if value is not None and parse_iso_duration_microseconds(value) <= 0:
            raise ValueError("retention must be a positive ISO-8601 duration")
        return value


def capture_contract_input_template(*, expanded: bool = False) -> dict[str, object]:
    record: dict[str, object] = {"name": "example.observation", "shape": "http_response"}
    if expanded:
        record.update(
            grade="observed",
            replay="attested",
            retention="P30D",
            erasable=False,
            max_bytes=16 * 1024 * 1024,
            selector_privacy="direct_allowed",
        )
    return record


def _lower_capture_contract_input(
    record: CaptureContractInputRecord, *, predecessor: CaptureContractAny | None = None
) -> CaptureContract:
    """New definitions default once; omitted edit fields retain accepted values."""
    if any(getattr(record, name) is None for name in record.model_fields_set - {"shape"}):
        raise CaptureFormatError(
            "CaptureContract input fields cannot be null; omit an unchanged field"
        )
    if predecessor is None:
        if record.shape is None:
            raise CaptureFormatError("a new CaptureContract requires shape")
        replay: CaptureReplay = record.replay or (
            "attested" if record.shape == "http_response" else "exact"
        )
        contract = CaptureContract(
            identity=ArtifactIdentity(kind="CaptureContract", name=record.name),
            shape=record.shape,
            replay=replay,
            components=capture_shape_components(record.shape, replay),
            epistemic_grade=record.grade or "observed",
            decoding=record.decoding,
            selection_budget=CaptureSelectionBudget(
                max_bytes=record.max_bytes or 16 * 1024 * 1024, max_rows=1, max_items=1
            ),
            retention_erasure_policy=CaptureRetentionErasurePolicy(
                body_retention="required_for_duration",
                minimum_retention=CanonicalDuration(
                    microseconds=parse_iso_duration_microseconds(record.retention or "P30D")
                ),
                erasure="prohibited",
                selector_privacy=record.selector_privacy or "direct_allowed",
            ),
            lifecycle=ArtifactLifecycle(state=record.lifecycle or "live"),
        )
        if record.erasable:
            erased = authorize_erasure(contract)
            assert isinstance(erased, CaptureContract)
            contract = erased
        return contract
    previous: CaptureContractAny = predecessor
    if isinstance(predecessor, CaptureContractV1):
        if predecessor.identity == COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity:
            previous = predecessor
            predecessor = builtin_contract_for_write(
                COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, previous, named=False
            )
        else:
            converted = None
            for shape in ("http_response", "procedure_output", "foreign_span", "database_snapshot"):
                for replay in ("exact", "attested"):
                    components = capture_shape_components(shape, replay)
                    if all(
                        getattr(predecessor, field) == getattr(components, field)
                        for field in components.model_fields
                    ):
                        converted = CaptureContract(
                            identity=predecessor.identity,
                            shape=shape,
                            replay=replay,
                            components=components,
                            selection_budget=predecessor.selection_budget,
                            retention_erasure_policy=capture_retention_policy(predecessor),
                            epistemic_grade=predecessor.epistemic_grade,
                            lifecycle=predecessor.lifecycle,
                        )
            if converted is None:
                raise CaptureFormatError(
                    "historical components have no compatible light shape; use a new name"
                )
            previous = predecessor
            predecessor = converted
    else:
        previous = predecessor
    if record.name != predecessor.identity.name:
        raise CaptureFormatError("a CaptureContract edit must retain its name")
    updates = {
        name: getattr(record, name)
        for name in ("shape", "replay")
        if name in record.model_fields_set
    }
    if "grade" in record.model_fields_set:
        updates["epistemic_grade"] = record.grade
    if "decoding" in record.model_fields_set:
        updates["decoding"] = record.decoding
    if "max_bytes" in record.model_fields_set:
        updates["selection_budget"] = predecessor.selection_budget.model_copy(
            update={"max_bytes": record.max_bytes}
        )
    policy = capture_retention_policy(predecessor)
    if "retention" in record.model_fields_set:
        assert record.retention is not None
        policy = policy.model_copy(
            update={
                "body_retention": "required_for_duration",
                "minimum_retention": CanonicalDuration(
                    microseconds=parse_iso_duration_microseconds(record.retention)
                ),
            }
        )
    if "selector_privacy" in record.model_fields_set:
        policy = policy.model_copy(update={"selector_privacy": record.selector_privacy})
    if "erasable" in record.model_fields_set and record.erasable != (
        policy.erasure == "authorized_by_rule"
    ):
        raise CaptureFormatError(
            "cruxible.capture_contract.incompatible_successor: erasure changed; use a new name"
        )
    updates.update(
        retention_erasure_policy=policy,
        lifecycle=ArtifactLifecycle(
            state=record.lifecycle or predecessor.lifecycle.state,
            predecessor_digest=capture_contract_digest(previous).tagged,
        ),
    )
    contract = CaptureContract.model_validate({**predecessor.model_dump(mode="python"), **updates})
    broken = capture_contract_successor_break(predecessor, contract)
    if broken and contract.lifecycle.state != "retired":
        raise CaptureFormatError(
            f"cruxible.capture_contract.incompatible_successor: {broken} changed; use a new name"
        )
    return contract


def lower_capture_contract_input(
    record: CaptureContractInputRecord,
    *,
    predecessor: CaptureContractAny | None = None,
    named: bool = False,
) -> CaptureContractAny:
    from cruxible_client.contracts.captures import (
        NamedCaptureContract,
        capture_components,
        name_capture_contract,
    )

    original = predecessor
    if isinstance(predecessor, NamedCaptureContract):
        predecessor = CaptureContract.model_validate(
            {
                **predecessor.model_dump(
                    mode="python",
                    exclude={"artifact_format", "components", "retention_erasure_policy"},
                ),
                "components": capture_components(predecessor),
                "retention_erasure_policy": capture_retention_policy(predecessor),
            }
        )
    result = _lower_capture_contract_input(record, predecessor=predecessor)
    if original is not None:
        result = result.model_copy(
            update={
                "lifecycle": result.lifecycle.model_copy(
                    update={
                        "predecessor_digest": capture_contract_digest(original).tagged,
                    }
                )
            }
        )
    return name_capture_contract(result) if named else result
