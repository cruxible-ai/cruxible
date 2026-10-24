"""Authored CaptureContracts; the engine expands the closed shape vocabulary."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    CanonicalDuration,
    CaptureContract,
    CaptureContractAny,
    CaptureFormatError,
    CaptureReplay,
    CaptureRetentionErasurePolicy,
    CaptureSelectionBudget,
    CaptureShape,
    authorize_erasure,
    capture_contract_digest,
    capture_contract_path,
    capture_contract_successor_break,
    capture_shape_components,
)
from cruxible_client.contracts.temporal import parse_iso_duration_microseconds


class CaptureContractInputRecord(BaseModel):
    """A new name and shape, or a merge patch of an accepted contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    shape: CaptureShape | None = None
    grade: Literal["observed", "derived", "predicted"] | None = None
    replay: CaptureReplay | None = None
    retention: str | None = None
    erasable: bool | None = None
    max_bytes: int | None = Field(default=None, ge=1)
    selector_privacy: Literal["direct_allowed", "pseudonymous_required"] | None = None
    lifecycle: Literal["live", "retired"] | None = None

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


def lower_capture_contract_input(
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
    if not isinstance(predecessor, CaptureContract):
        raise CaptureFormatError(
            "edit a historical v1 contract through its compatible v2 successor first"
        )
    if record.name != predecessor.identity.name:
        raise CaptureFormatError("a CaptureContract edit must retain its name")
    updates = {
        name: getattr(record, name)
        for name in ("shape", "replay")
        if name in record.model_fields_set
    }
    if "grade" in record.model_fields_set:
        updates["epistemic_grade"] = record.grade
    if "max_bytes" in record.model_fields_set:
        updates["selection_budget"] = predecessor.selection_budget.model_copy(
            update={"max_bytes": record.max_bytes}
        )
    policy = predecessor.retention_erasure_policy
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
            predecessor_digest=capture_contract_digest(predecessor).tagged,
        ),
    )
    contract = CaptureContract.model_validate({**predecessor.model_dump(mode="python"), **updates})
    broken = capture_contract_successor_break(predecessor, contract)
    if broken and contract.lifecycle.state != "retired":
        raise CaptureFormatError(
            f"cruxible.capture_contract.incompatible_successor: {broken} changed; use a new name"
        )
    return contract
